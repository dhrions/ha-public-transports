"""Per-terminus filtering: derive the terminuses circulating for one already-chosen
(line, sense), and turn a "which terminus(es)" multi-select into 1..N sense specs.

Pure logic, no HA form/step code — shared by PublicTransportsConfigFlow (flow.py) and
PublicTransportsOptionsFlowHandler (options_flow.py), which each wire it into their own
async_step_select_destination since the two flows finish very differently (create a new
entry vs merge into existing options), but build the same options and specs from it.
"""

from ..coordinator import normalize_destination, scalar

# Sentinelle « pas de filtre terminus » : garde le capteur agrégé (tous les terminus du
# sens), en plus des capteurs par terminus qu'on peut cocher à côté.
ALL_TERMINUSES = "__all_terminuses__"


def destinations_from_calls(calls, line_ref, direction_ref):
    """Distinct terminuses (destination_name) circulating for one (line, sense).

    Unlike _directions_from_calls (which groups several terminuses under one sense
    label), this lists them individually — the finer split this step lets the user opt
    into. Deduplicated on normalize_destination so accent/casing producer variants of the
    same real terminus don't appear as two options; the first raw spelling seen is kept as
    both the filter value and the label.
    """
    destinations = {}
    seen_folded = set()
    for call in calls:
        if line_ref and scalar(call.line_ref) != line_ref:
            continue
        if direction_ref and scalar(call.direction_ref) != direction_ref:
            continue
        name = scalar(call.destination_name)
        if not name:
            continue
        folded = normalize_destination(name)
        if folded in seen_folded:
            continue
        seen_folded.add(folded)
        destinations[name] = name
    return destinations


def destination_options(destinations: dict) -> dict:
    """Multi-select options for the step: the aggregate choice, then one per terminus."""
    return {ALL_TERMINUSES: "Tous les terminus (capteur unique)", **destinations}


def specs_from_destination_choice(spec_builder, destinations: dict, chosen: list[str]) -> list[dict]:
    """Turn the checked keys of destination_options into 1..N specs.

    spec_builder(destination_filter, destination_label) builds one full sense spec —
    passed in rather than called directly here since each flow's _spec()/equivalent also
    needs its own line/direction/stop_code state. An empty selection falls back to the
    aggregate alone (never zero sensors) rather than being treated as an error, consistent
    with the "case vide = pas de filtre" convention used elsewhere in this config flow.
    """
    if not chosen:
        chosen = [ALL_TERMINUSES]
    specs = []
    for key in chosen:
        if key == ALL_TERMINUSES:
            specs.append(spec_builder(None, None))
        else:
            specs.append(spec_builder(key, destinations.get(key, key)))
    return specs
