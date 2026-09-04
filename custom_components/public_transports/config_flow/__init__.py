"""Config flow package, split from a single 1127-line config_flow.py into:

- helpers.py : selectors, line/direction extraction, IDFM line-name resolution.
- destinations.py : per-terminus filtering — pure logic shared by flow.py and
  options_flow.py's respective async_step_select_destination.
- flow.py : the initial config flow (PublicTransportsConfigFlow).
- options_flow.py : the post-setup options flow (PublicTransportsOptionsFlowHandler).

Home Assistant only needs `<domain>.config_flow` to be importable for the ConfigFlow
subclass's `domain=DOMAIN` decoration to register it — re-exporting here keeps that single
entry point and keeps `from .config_flow import PublicTransportsConfigFlow` working
unchanged from __init__.py. The re-exports below also keep every existing import path
(tests included) working unchanged after the split.
"""

from .destinations import ALL_TERMINUSES, destination_options, destinations_from_calls, specs_from_destination_choice
from .flow import PublicTransportsConfigFlow
from .helpers import (
    ALL_LINES,
    BOTH_SENSES,
    _directions_from_calls,
    _lines_from_calls,
    dropdown,
    probe_available_passages,
    resolve_line_label,
)
from .options_flow import PublicTransportsOptionsFlowHandler

__all__ = [
    "ALL_LINES",
    "ALL_TERMINUSES",
    "BOTH_SENSES",
    "PublicTransportsConfigFlow",
    "PublicTransportsOptionsFlowHandler",
    "_directions_from_calls",
    "_lines_from_calls",
    "destination_options",
    "destinations_from_calls",
    "dropdown",
    "probe_available_passages",
    "resolve_line_label",
    "specs_from_destination_choice",
]
