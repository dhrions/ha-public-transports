"""Targeted tests for PublicTransportsConfigFlow — pure functions and branches where a
real bug was found live (stop_code fallback, sense/line splitting), not a full step walk.
"""

from unittest.mock import patch

import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry
from siri_lite.models import MonitoredCall, RateLimitInfo

import aiohttp

from custom_components.public_transports.config_flow import (
    ALL_LINES,
    ALL_TERMINUSES,
    BOTH_SENSES,
    PublicTransportsConfigFlow,
    PublicTransportsOptionsFlowHandler,
    _directions_from_calls,
    _lines_from_calls,
    destination_options,
    destinations_from_calls,
    probe_available_passages,
    resolve_line_label,
    specs_from_destination_choice,
)
from custom_components.public_transports.const import DOMAIN


class _FakeResponse:
    """Minimal aiohttp response stand-in, usable as `async with session.get(...)`."""

    def __init__(self, status, data=None):
        self.status = status
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in for resolve_line_label's HTTP call."""

    def __init__(self, response=None, get_exc=None):
        self._response = response
        self._get_exc = get_exc

    def get(self, url, params=None):
        if self._get_exc:
            raise self._get_exc
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

# Already-resolved published_line_name (!= line_ref) so resolve_line_names() never calls
# resolve_line_label() (the only aiohttp-touching path in these steps) — see config_flow.py.
CALLS_TWO_LINES_TWO_SENSES = [
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Asnières"),
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Retour", destination_name="Châtillon Montrouge"),
    MonitoredCall(line_ref="C01384", published_line_name="6", direction_ref="Aller", destination_name="Nation"),
]

# Une ligne fourchue à un seul sens qui dessert deux terminus distincts (ex. métro 13
# nord), pour exercer le raffinement par terminus (select_destination).
CALLS_FORKED_DESTINATION = [
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Asnières-Gennevilliers"),
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Saint-Denis Université"),
]

# Trois lignes (une fourchue) pour exercer un vrai sous-ensemble strict au step ligne.
CALLS_THREE_LINES = [
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Asnières"),
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Retour", destination_name="Châtillon Montrouge"),
    MonitoredCall(line_ref="C01384", published_line_name="6", direction_ref="Aller", destination_name="Nation"),
    MonitoredCall(line_ref="C01385", published_line_name="4", direction_ref="Aller", destination_name="Bagneux"),
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


def test_directions_from_calls_caps_terminuses_in_label_beyond_the_limit():
    """Merging across a whole multi-line pole (line_ref=None, ex. "Toutes les lignes")
    can pack many unrelated lines' terminuses into the same sense — unreadable if joined
    in full, as seen live (2026-08-22): a 6-terminus label on one line. Capped with a
    "+N autres" suffix past the limit; unaffected below it (single forked line case).
    """
    calls = [
        MonitoredCall(line_ref=f"L{i}", direction_ref="Aller", destination_name=name)
        for i, name in enumerate(["Asnières", "Clamart", "Auteuil", "Champerret", "Saint-Denis", "Vanves"])
    ]

    senses = _directions_from_calls(calls)

    assert senses["Aller"] == "Asnières / Auteuil / Champerret (+3 autres)"


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


async def test_select_line_all_lines_creates_one_spec_per_line_and_sense(hass):
    """Selecting every line (the default) must fan out per (line x real sense), with a
    single merged spec for lines that only expose one sense — reproduces the manual
    Châtelet-Les Halles test (3 lines, one forked -> N sensors).
    """
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]

    result = await flow.async_step_select_line({"lines": ["C01383", "C01384"]})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 3  # line 13: 2 senses, line 6: 1 (merged)
    line_13_specs = [s for s in specs if s["line_filter"] == "C01383"]
    assert {s["direction_filter"] for s in line_13_specs} == {"Aller", "Retour"}
    line_6_specs = [s for s in specs if s["line_filter"] == "C01384"]
    assert len(line_6_specs) == 1
    assert line_6_specs[0]["direction_filter"] == "Aller"


async def test_select_line_offers_a_multi_select_of_concrete_lines_never_all_lines(hass):
    """A splittable stop (single code or pole) must offer a MULTI-select of the concrete
    lines — the way to cover the whole stop, or a subset, one sensor per line. Reproduces
    the live bug (2026-08-22): the Gaîté pole offered only single-line choices. ALL_LINES
    (a merged sensor mixing lines) must NEVER be offered — no practical use (user feedback).
    """
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:", "STIF:StopArea:SP:45103:"]
    flow.pole_codes = ["STIF:StopArea:SP:45102:", "STIF:StopArea:SP:45103:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"

    result = await flow.async_step_select_line()

    assert result["type"] == "form"
    lines_selector = result["data_schema"].schema[vol.Required("lines")]
    assert lines_selector.config["multiple"] is True
    values = {opt["value"] for opt in lines_selector.config["options"]}
    assert values == {"C01383", "C01384"}
    assert ALL_LINES not in values


async def test_select_line_all_lines_on_a_pole_reuses_merged_codes_per_line(hass):
    """Selecting all lines on a pole must fan out per (line x sense) like a single stop, but
    every spec rereads ALL the pole codes (stop_codes) and filters on its own line — so all
    specs still share one coordinator (no extra API calls) while each line gets its sensor(s).
    Reproduces the user's ask (2026-08-22): "2 entités par ligne (1 par sens)" on a pole.
    """
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    pole = ["STIF:StopArea:SP:45102:", "STIF:StopArea:SP:45103:"]
    flow.candidate_codes = list(pole)
    flow.pole_codes = list(pole)
    flow.stop_code = pole[0]

    result = await flow.async_step_select_line({"lines": ["C01383", "C01384"]})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 3  # line 13: 2 senses, line 6: 1 (merged)
    # Every spec reads the whole pole (one shared coordinator), filtered per line.
    assert all(s["stop_codes"] == pole for s in specs)
    assert {s["direction_filter"] for s in specs if s["line_filter"] == "C01383"} == {"Aller", "Retour"}
    line_6 = [s for s in specs if s["line_filter"] == "C01384"]
    assert len(line_6) == 1 and line_6[0]["direction_filter"] == "Aller"


async def test_select_line_subset_splits_only_the_chosen_lines(hass):
    """A strict subset (2 of 3 lines) must create one sensor per CHOSEN line×sense and
    leave the unchosen line out entirely — the feature ask (suivre 2 lignes sur 5)."""
    flow = _flow(hass)
    flow.available_calls = CALLS_THREE_LINES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]

    result = await flow.async_step_select_line({"lines": ["C01383", "C01384"]})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    chosen_lines = {s["line_filter"] for s in specs}
    assert chosen_lines == {"C01383", "C01384"}  # line 4 (C01385) excluded
    assert len(specs) == 3  # 13: 2 senses, 6: 1 sense


async def test_select_line_empty_selection_is_treated_as_all_lines(hass):
    """Submitting with nothing ticked must not error — it means "toutes les lignes"."""
    flow = _flow(hass)
    flow.available_calls = CALLS_THREE_LINES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]

    result = await flow.async_step_select_line({"lines": []})

    assert result["type"] == "create_entry"
    assert {s["line_filter"] for s in result["data"]["senses"]} == {"C01383", "C01384", "C01385"}


async def test_select_line_picking_one_line_still_goes_to_sense_selection(hass):
    """Selecting exactly ONE line (a single-element list) must proceed to sense selection
    filtered on that line, preserving the precise "one sense / both senses" choice."""
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:", "STIF:StopArea:SP:45103:"]
    flow.pole_codes = ["STIF:StopArea:SP:45102:", "STIF:StopArea:SP:45103:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"

    line_result = await flow.async_step_select_line({"lines": ["C01383"]})
    assert flow.line_filter == "C01383"
    assert line_result["type"] == "form"
    assert line_result["step_id"] == "select_direction"

    result = await flow.async_step_select_direction({"direction": BOTH_SENSES})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 2
    assert all(s["line_filter"] == "C01383" for s in specs)
    assert {s["direction_filter"] for s in specs} == {"Aller", "Retour"}


async def test_select_line_defaults_to_all_lines_selected(hass):
    """The lines multi-select must pre-tick every line (the former "un capteur par ligne"
    default), so validating without change covers the whole stop."""
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]

    result = await flow.async_step_select_line()

    lines_key = next(k for k in result["data_schema"].schema if k == "lines")
    assert set(lines_key.default()) == {"C01383", "C01384"}


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


def test_destinations_from_calls_dedupes_accent_and_case_variants():
    """Two producer spellings of the same real terminus must collapse to one option."""
    calls = [
        MonitoredCall(line_ref="C01383", direction_ref="Aller", destination_name="Saint-Denis - Université"),
        MonitoredCall(line_ref="C01383", direction_ref="Aller", destination_name="saint-denis - universite"),
        MonitoredCall(line_ref="C01383", direction_ref="Aller", destination_name="Asnières-Gennevilliers"),
    ]
    destinations = destinations_from_calls(calls, "C01383", "Aller")
    assert destinations == {
        "Saint-Denis - Université": "Saint-Denis - Université",
        "Asnières-Gennevilliers": "Asnières-Gennevilliers",
    }


def test_destinations_from_calls_filters_by_line_and_direction():
    destinations = destinations_from_calls(CALLS_TWO_LINES_TWO_SENSES, "C01383", "Retour")
    assert destinations == {"Châtillon Montrouge": "Châtillon Montrouge"}


def test_specs_from_destination_choice_empty_selection_falls_back_to_aggregate():
    specs = specs_from_destination_choice(lambda f, l: {"filter": f, "label": l}, {"Nation": "Nation"}, [])
    assert specs == [{"filter": None, "label": None}]


def test_specs_from_destination_choice_all_checked_builds_aggregate_plus_each_terminus():
    destinations = {"Asnières-Gennevilliers": "Asnières-Gennevilliers", "Saint-Denis Université": "Saint-Denis Université"}
    options = destination_options(destinations)
    specs = specs_from_destination_choice(
        lambda f, l: {"filter": f, "label": l}, destinations, list(options)
    )
    assert len(specs) == 3
    assert {"filter": None, "label": None} in specs
    assert {"filter": "Asnières-Gennevilliers", "label": "Asnières-Gennevilliers"} in specs
    assert {"filter": "Saint-Denis Université", "label": "Saint-Denis Université"} in specs


async def test_select_direction_single_sense_forked_destination_routes_to_select_destination(hass):
    """A single (implicit) sense that still forks into ≥2 terminuses must ask which
    terminus(es) to track, instead of creating the entry right away."""
    flow = _flow(hass)
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_FORKED_DESTINATION

    result = await flow.async_step_select_direction()

    assert result["type"] == "form"
    assert result["step_id"] == "select_destination"
    destinations_key = next(k for k in result["data_schema"].schema if k == "destinations")
    assert set(destinations_key.default()) == {
        ALL_TERMINUSES, "Asnières-Gennevilliers", "Saint-Denis Université",
    }


async def test_select_destination_all_checked_creates_aggregate_and_per_terminus_specs(hass):
    """The default (everything ticked) must yield 3 sensors: the aggregate plus one per
    terminus — the actual feature ask."""
    flow = _flow(hass)
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_FORKED_DESTINATION
    await flow.async_step_select_direction()  # populates self._pending_direction/_destinations

    result = await flow.async_step_select_destination({
        "destinations": [ALL_TERMINUSES, "Asnières-Gennevilliers", "Saint-Denis Université"],
    })

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 3
    assert {s.get("destination_filter") for s in specs} == {None, "Asnières-Gennevilliers", "Saint-Denis Université"}
    aggregate = next(s for s in specs if s.get("destination_filter") is None)
    assert aggregate["line_filter"] == "C01383"
    per_terminus = next(s for s in specs if s.get("destination_filter") == "Saint-Denis Université")
    assert per_terminus["destination_label"] == "Saint-Denis Université"


async def test_select_destination_unchecking_aggregate_keeps_only_chosen_termini(hass):
    flow = _flow(hass)
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_FORKED_DESTINATION
    await flow.async_step_select_direction()

    result = await flow.async_step_select_destination({"destinations": ["Saint-Denis Université"]})

    specs = result["data"]["senses"]
    assert len(specs) == 1
    assert specs[0]["destination_filter"] == "Saint-Denis Université"


async def test_select_direction_both_senses_does_not_offer_destination_refinement(hass):
    """"Les deux sens" must not layer a destination step on top — it already fans out to
    2 sensors, and the destination refinement is scoped to a single retained sense."""
    flow = _flow(hass)
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES

    result = await flow.async_step_select_direction({"direction": BOTH_SENSES})

    assert result["type"] == "create_entry"
    assert len(result["data"]["senses"]) == 2


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


OPTIONS_ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "senses": [{"stop_code": "43A"}],
}


def _options_flow(hass, entry):
    flow = PublicTransportsOptionsFlowHandler(entry)
    flow.hass = hass
    return flow


async def test_options_flow_shows_daily_estimate_on_first_render(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], RateLimitInfo(limit_day=1000000)),
    ):
        result = await flow.async_step_init()

    assert result["type"] == "form"
    assert "estimate" in result["description_placeholders"]
    assert "1 000 000" in result["description_placeholders"]["limit_text"]


async def test_options_flow_rejects_projection_exceeding_known_quota(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], RateLimitInfo(limit_day=100)),
    ):
        # 1 requête/seconde, 1 code -> 86 400/jour, largement au-dessus du quota de 100.
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "1"})

    assert result["type"] == "form"
    assert result["errors"]["base"] == "quota_exceeded"


async def test_options_flow_accepts_projection_within_known_quota(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], RateLimitInfo(limit_day=1000000)),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "60"})

    assert result["type"] == "create_entry"
    assert result["data"]["scan_interval"] == 60


async def test_options_flow_single_sense_forked_destination_routes_to_select_destination(hass):
    """Re-choosing a single sense that still forks into ≥2 terminuses must ask which to
    track, mirroring the initial config flow's select_destination step."""
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=(CALLS_FORKED_DESTINATION, None),
    ):
        result = await flow.async_step_init({"line": "C01383", "direction": "Aller", "scan_interval": "60"})

    assert result["type"] == "form"
    assert result["step_id"] == "select_destination"
    destinations_key = next(k for k in result["data_schema"].schema if k == "destinations")
    assert set(destinations_key.default()) == {
        ALL_TERMINUSES, "Asnières-Gennevilliers", "Saint-Denis Université",
    }


async def test_options_flow_select_destination_all_checked_builds_aggregate_and_per_terminus(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=(CALLS_FORKED_DESTINATION, None),
    ):
        await flow.async_step_init({"line": "C01383", "direction": "Aller", "scan_interval": "60"})
        result = await flow.async_step_select_destination({
            "destinations": [ALL_TERMINUSES, "Asnières-Gennevilliers", "Saint-Denis Université"],
        })

    assert result["type"] == "create_entry"
    assert result["data"]["scan_interval"] == 60
    specs = result["data"]["senses"]
    assert len(specs) == 3
    assert {s.get("destination_filter") for s in specs} == {None, "Asnières-Gennevilliers", "Saint-Denis Université"}
    assert all(s["line_filter"] == "C01383" for s in specs)


async def test_options_flow_select_destination_unchecking_aggregate_keeps_only_chosen(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=(CALLS_FORKED_DESTINATION, None),
    ):
        await flow.async_step_init({"line": "C01383", "direction": "Aller", "scan_interval": "60"})
        result = await flow.async_step_select_destination({"destinations": ["Saint-Denis Université"]})

    specs = result["data"]["senses"]
    assert len(specs) == 1
    assert specs[0]["destination_filter"] == "Saint-Denis Université"


def test_spec_labels_prefers_destination_label_over_direction_label():
    """The advanced per-sensor walking-time step must label a per-terminus spec by its
    terminus, not by the sense's (redundant, joined) direction label."""
    specs = [{
        "line_name": "13",
        "direction_label": "Asnières-Gennevilliers / Saint-Denis Université",
        "destination_label": "Saint-Denis Université",
    }]

    labels = PublicTransportsOptionsFlowHandler._spec_labels(specs)

    assert labels == ["13 → Saint-Denis Université"]


async def test_options_flow_accepts_custom_scan_interval_outside_preset_list(hass):
    """scan_interval est un combobox (custom_value=True) : une valeur absente de
    SCAN_INTERVAL_OPTIONS (ex. 45s) doit être acceptée, pas seulement les présets."""
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], RateLimitInfo(limit_day=1000000)),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "45"})

    assert result["type"] == "create_entry"
    assert result["data"]["scan_interval"] == 45


async def test_options_flow_rejects_non_numeric_scan_interval(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "abc"})

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_scan_interval"


async def test_options_flow_rejects_scan_interval_below_minimum(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "0"})

    assert result["type"] == "form"
    assert result["errors"]["base"] == "scan_interval_out_of_range"


async def test_options_flow_rejects_scan_interval_above_maximum(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "99999"})

    assert result["type"] == "form"
    assert result["errors"]["base"] == "scan_interval_out_of_range"


async def test_options_flow_saves_without_quota_check_when_unknown(hass):
    """CTS doesn't expose rate-limit headers — no limit_day means no comparison possible."""
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({"line": "__all__", "direction": "__all__", "scan_interval": "1"})

    assert result["type"] == "create_entry"


async def test_options_flow_rejects_incomplete_quiet_hours(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({
            "line": "__all__",
            "direction": "__all__",
            "scan_interval": "60",
            "quiet_hours_start": "22:00:00",
        })

    assert result["type"] == "form"
    assert result["errors"]["base"] == "quiet_hours_incomplete"


async def test_options_flow_saves_valid_quiet_hours(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({
            "line": "__all__",
            "direction": "__all__",
            "scan_interval": "60",
            "quiet_hours_start": "22:00:00",
            "quiet_hours_end": "06:00:00",
        })

    assert result["type"] == "create_entry"
    assert result["data"]["quiet_hours_start"] == "22:00:00"
    assert result["data"]["quiet_hours_end"] == "06:00:00"


async def test_options_flow_saves_entry_level_walking_time(hass):
    """The entry-level walking time (offset zone/arrêt) is persisted in options."""
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({
            "line": "__all__",
            "direction": "__all__",
            "scan_interval": "60",
            "walking_time": 8,
        })

    assert result["type"] == "create_entry"
    assert result["data"]["walking_time"] == 8


async def test_options_flow_single_sensor_has_no_per_sense_toggle(hass):
    """A single-sensor entry offers only the entry-level walking time — a per-sense override
    would say nothing more than that default, so the advanced toggle is hidden."""
    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init()

    assert "walking_time" in result["data_schema"].schema
    assert "configure_per_sense" not in result["data_schema"].schema


async def test_options_flow_per_sense_advanced_step_sets_only_the_chosen_spec(hass):
    """Ticking "configure_per_sense" on a multi-sensor entry routes to the advanced step,
    where a value on one sensor's field overrides just that spec (the others inherit — no
    walking_time key)."""
    entry = MockConfigEntry(domain=DOMAIN, data=SPLIT_BY_LINE_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        advanced = await flow.async_step_init({
            "scan_interval": "60",
            "walking_time": 5,
            "configure_per_sense": True,
        })

    assert advanced["type"] == "form"
    assert advanced["step_id"] == "walking_advanced"
    # Les clés de champ sont les libellés des capteurs (lignes 13 / 58 / 59).
    assert set(advanced["data_schema"].schema) == {"13", "58", "59"}

    result = await flow.async_step_walking_advanced({"58": 3})

    assert result["type"] == "create_entry"
    specs = {s["line_name"]: s for s in result["data"]["senses"]}
    assert specs["58"]["walking_time"] == 3
    assert "walking_time" not in specs["13"]  # hérite du défaut d'entrée
    assert "walking_time" not in specs["59"]
    assert result["data"]["walking_time"] == 5  # défaut d'entrée conservé


SPLIT_BY_LINE_ENTRY_DATA = {
    "city": "Paris",
    "transit_company": "IDF Mobilités / RATP",
    "api_token": "fake-token",
    "stop_name": "Gaîté",
    "senses": [
        {"stop_code": "43046", "stop_codes": ["43046", "59070", "463915"], "line_filter": "C01383", "line_name": "13"},
        {"stop_code": "43046", "stop_codes": ["43046", "59070", "463915"], "line_filter": "C01120", "line_name": "58"},
        {"stop_code": "43046", "stop_codes": ["43046", "59070", "463915"], "line_filter": "C02245", "line_name": "59"},
    ],
}


async def test_options_flow_split_by_line_has_no_line_field():
    """A "one sensor per line" pole entry can't offer a single "line" dropdown — there's
    no one value to show, and submitting any would apply it to every spec (see the next
    test for the incident this used to cause).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=SPLIT_BY_LINE_ENTRY_DATA)
    flow = PublicTransportsOptionsFlowHandler(entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init()

    assert result["type"] == "form"
    assert "line" not in result["data_schema"].schema


async def test_options_flow_split_by_line_preserves_each_specs_own_line_filter(hass):
    """Incident 2026-08-23: submitting this form (even just to change scan_interval) used
    to overwrite every spec's line_filter with whatever the (single, now-removed) "line"
    dropdown showed — collapsing a 3-line pole entry down to one line across all sensors.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=SPLIT_BY_LINE_ENTRY_DATA)
    entry.add_to_hass(hass)
    flow = _options_flow(hass, entry)

    with patch(
        "custom_components.public_transports.config_flow.options_flow.probe_available_passages",
        return_value=([], None),
    ):
        result = await flow.async_step_init({"scan_interval": "60"})

    assert result["type"] == "create_entry"
    saved_line_filters = [spec["line_filter"] for spec in result["data"]["senses"]]
    assert saved_line_filters == ["C01383", "C01120", "C02245"]


def test_options_flow_init_never_assigns_the_config_entry_property(hass):
    """`self.config_entry = ...` crashes with an outright AttributeError on HA core
    versions that dropped OptionsFlow.config_entry's setter entirely (observed in
    production 2026-08-22 — every options-flow open 500'd). The repo's pinned test
    dependency still carries the setter (deprecated-but-present), so this bug shipped
    silently through the normal test suite; only removing the setter here, matching
    where HA core is headed, reproduces it locally.
    """
    from homeassistant import config_entries as ha_config_entries

    entry = MockConfigEntry(domain=DOMAIN, data=OPTIONS_ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch.object(ha_config_entries.OptionsFlow, "config_entry", property(lambda self: self._config_entry)):
        flow = PublicTransportsOptionsFlowHandler(entry)

    assert flow.config_entry is entry


async def test_user_step_renders_via_the_real_flow_manager(hass):
    """"Ajouter une entrée" crashed in production 2026-08-22 with an AttributeError on
    SelectSelectorMode.COMBOBOX, a member that doesn't exist (this repo's pinned test
    dependency doesn't have it either — cf. dropdown()'s docstring). Every prior test of
    this step called PublicTransportsConfigFlow methods directly, which never serializes
    the schema the way the real frontend does — going through
    hass.config_entries.flow.async_init() is what actually exercises that serialization.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"


async def test_user_step_rejects_a_custom_value_not_in_cities_data(hass):
    """The city field allows free-text entry (custom_value=True, restoring the old
    COMBOBOX-style UX) — a typed city absent from CITIES_DATA must be rejected with the
    existing invalid_city error, not KeyError on CITIES_DATA[self.city] downstream.
    """
    init = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    result = await hass.config_entries.flow.async_configure(
        init["flow_id"], {"city": "Nowhereville"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "invalid_city"


async def test_probe_available_passages_returns_empty_on_client_error(hass):
    """probe_available_passages must never let the config flow crash on a probe failure —
    any exception raised by the underlying SIRI client (network error, bad response...)
    must be caught and turned into ([], None), not propagated. Unlike the other tests of
    this file, the function itself is NOT mocked here — its own try/except is what's
    under test."""
    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        side_effect=ConnectionError("boom"),
    ):
        calls, rate_limit = await probe_available_passages(
            hass, "Compagnie des Transports Strasbourgeois", "fake-token", "43A"
        )

    assert calls == []
    assert rate_limit is None


async def test_probe_available_passages_returns_calls_and_rate_limit_on_success(hass):
    """Sanity check on the success path, so the error-path test above isn't the only
    coverage of this function's actual return shape. last_rate_limit is set as a side
    effect of the real fetch_next_calls (cf. SiriClient), so the fake must set it too
    rather than only stubbing the return value."""
    expected_calls = [MonitoredCall(line_ref="A")]

    def _fake_fetch(self):
        self.last_rate_limit = RateLimitInfo(remaining_day=42)
        return expected_calls

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        autospec=True,
        side_effect=_fake_fetch,
    ):
        calls, rate_limit = await probe_available_passages(
            hass, "Compagnie des Transports Strasbourgeois", "fake-token", "43A"
        )

    assert calls == expected_calls
    assert rate_limit.remaining_day == 42


async def test_resolve_line_label_returns_none_on_non_200_status(hass):
    """A non-200 response from the IDFM lines dataset must fall back to None (caller then
    keeps showing the raw LineRef) rather than crash on an unparseable body."""
    session = _FakeSession(response=_FakeResponse(status=404))
    with patch("aiohttp.ClientSession", return_value=session):
        result = await resolve_line_label(hass, "STIF:Line::C01383:")

    assert result is None


async def test_resolve_line_label_returns_none_on_client_error(hass):
    """The only path in these config-flow helpers that touches the network — a connection
    failure must be caught and turned into None, not propagated to the flow."""
    session = _FakeSession(get_exc=aiohttp.ClientConnectionError("boom"))
    with patch("aiohttp.ClientSession", return_value=session):
        result = await resolve_line_label(hass, "STIF:Line::C01383:")

    assert result is None


async def test_resolve_line_label_returns_the_resolved_name_on_success(hass):
    """Sanity check on the success path, so the two failure-path tests above aren't the
    only coverage of this function."""
    data = {"results": [{"route_long_name": "13", "mode": "Metro"}]}
    session = _FakeSession(response=_FakeResponse(status=200, data=data))
    with patch("aiohttp.ClientSession", return_value=session):
        result = await resolve_line_label(hass, "STIF:Line::C01383:")

    assert result == "13 (Metro)"
