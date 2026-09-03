"""Config-flow helpers: selectors, line/direction extraction from raw calls, and IDFM
line-name resolution. Shared by PublicTransportsConfigFlow and
PublicTransportsOptionsFlowHandler.
"""

import logging

import aiohttp
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    IDFM_LINES_API_URL,
    IDFM_ZONES_API_URL,
    MAX_WALKING_TIME_MINUTES,
    TRANSIT_COMPANIES,
)
from ..coordinator import build_siri_client, scalar

def dropdown(options, mode=None, custom_value=False, multiple=False):
    """Build a searchable selector from an {value: label} mapping.

    Replaces vol.In(...) so long lists (cities, hundreds of CTS stops) get a search box
    instead of a radio list. Labels are passed inline, sidestepping translation of
    dynamic values (stop/line names).

    custom_value=True gives combobox-like behaviour on top of DROPDOWN — free-text entry
    in addition to the suggested options — which is what a separate SelectSelectorMode
    .COMBOBOX used to be called upon for. That member doesn't exist in this HA version
    (nor in this repo's pinned test dependency, which has the same gap): calling it
    crashed "Ajouter une entrée" outright with an AttributeError in production
    2026-08-22, since HA moved this to a SelectSelectorConfig flag instead of a mode.

    multiple=True lets the user pick several values at once (the field then returns a
    list); used by the line step to follow a subset of an stop's lines.
    """
    if mode is None:
        mode = SelectSelectorMode.DROPDOWN
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=str(value), label=str(label))
                for value, label in options.items()
            ],
            mode=mode,
            custom_value=custom_value,
            multiple=multiple,
        )
    )

def _walking_time_selector():
    """A whole-minutes number box for a walking-time offset, bounded to [0, MAX].

    The UI enforces the range; entry_walking_time/spec_walking_time re-clamp at read so a
    hand-edited .storage value can't escape it either.
    """
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=MAX_WALKING_TIME_MINUTES,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


# Créez un logger spécifique pour votre intégration
_LOGGER = logging.getLogger(__name__)

# Sentinelles « pas de filtre » pour les listes déroulantes ligne / sens.
ALL_LINES = "__all__"
ALL_DIRECTIONS = "__all__"
# Sentinelle « les deux sens » : crée une entrée qui expose 2 capteurs (un par sens).
BOTH_SENSES = "__both__"

# Traduction des zdatype du référentiel IDFM zones-d-arrets, pour désambiguïser les
# arrêts homonymes (ex. "Gaîté" = une station de métro et un arrêt de bus distincts).
ZDATYPE_LABELS = {
    "metroStation": "métro",
    "railStation": "gare/RER",
    "onstreetBus": "bus",
    "onstreetTram": "tram",
    "coach": "car",
}


async def probe_available_passages(hass, transit_company, api_token, stop_code):
    """Probe stop-monitoring once to discover the lines/directions serving a stop.

    Runs the sync siri-lite client (same path as the coordinator) in an executor, so the
    config flow sees exactly the passages the sensor will. Returns ([], None) on any error
    or when nothing is currently circulating. The second element is the RateLimitInfo
    reported by this same call (None if the producer doesn't expose it, ex. CTS) — reused
    by the options flow to show/enforce a daily-quota estimate without an extra API call.
    """
    transit_info = TRANSIT_COMPANIES.get(transit_company, {})
    client = build_siri_client(transit_info, api_token, stop_code)
    try:
        calls = await hass.async_add_executor_job(client.fetch_next_calls)
        return calls, client.last_rate_limit
    except Exception as err:  # noqa: BLE001 - config flow must not crash on probe failure
        _LOGGER.error(f"Error probing stop for lines/directions: {err}")
        return [], None


def _lines_from_calls(calls):
    """Distinct {line_ref: published_line_name} from a list of MonitoredCall."""
    lines = {}
    for call in calls:
        line_ref = scalar(call.line_ref)
        if line_ref:
            lines[line_ref] = scalar(call.published_line_name) or line_ref
    return lines


_MAX_TERMINUSES_IN_LABEL = 3


def _directions_from_calls(calls, line_ref=None):
    """Group passages by real sense (SIRI DirectionRef), optionally within one line.

    Returns {direction_ref: label} where label is composed from the distinct terminuses
    seen for that sense (ex. "Asnières… / Saint-Denis…" vs "Châtillon Montrouge"). This is
    the 2-way sense the user picks — the per-vehicle terminus stays for the sensor display.
    A forked line (ex. metro 13) has several terminuses per sense, hence the join — capped
    at _MAX_TERMINUSES_IN_LABEL with a "+N autres" suffix beyond that, since line_ref=None
    (ex. "Toutes les lignes" on a multi-line pole) can merge dozens of unrelated lines'
    terminuses into the same sense, unlike a single forked line's 2-3.
    """
    senses = {}
    for call in calls:
        if line_ref and scalar(call.line_ref) != line_ref:
            continue
        direction_ref = scalar(call.direction_ref)
        if not direction_ref:
            continue
        terminus = scalar(call.destination_name)
        bucket = senses.setdefault(direction_ref, [])
        if terminus and terminus not in bucket:
            bucket.append(terminus)

    def _label(direction_ref, terminuses):
        shown = sorted(terminuses)[:_MAX_TERMINUSES_IN_LABEL]
        extra = len(terminuses) - len(shown)
        label = " / ".join(shown) or direction_ref
        return f"{label} (+{extra} autres)" if extra > 0 else label

    return {
        direction_ref: _label(direction_ref, terminuses)
        for direction_ref, terminuses in senses.items()
    }


async def resolve_line_label(hass, line_ref):
    """Look up a human-readable line name for a raw SIRI LineRef.

    Some producers (PRIM, unlike CTS) leave PublishedLineName empty in stop-monitoring
    responses, so the config flow would otherwise show the raw internal code (ex.
    "STIF:Line::C01383:"). The public IDFM "arrets-lignes" dataset maps the code segment
    to a readable name (ex. id "IDFM:C01383" -> route_long_name "13", mode "Metro") —
    verified 2026-08-19. Returns None if not found (caller falls back to the raw ref).
    """
    parts = [part for part in line_ref.split(":") if part]
    if not parts:
        return None
    params = {"where": f'id="IDFM:{parts[-1]}"', "limit": 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(IDFM_LINES_API_URL, params=params) as response:
                if response.status != 200:
                    return None
                data = await response.json()
    except aiohttp.ClientError as err:
        _LOGGER.error(f"HTTP error occurred while resolving line name: {err}")
        return None

    results = data.get("results", [])
    if not results:
        return None
    name = results[0].get("route_long_name") or results[0].get("shortname")
    mode = results[0].get("mode")
    return f"{name} ({mode})" if name and mode else name


async def resolve_line_names(hass, lines):
    """Enrich a {line_ref: label} dict, resolving labels that fell back to the raw ref."""
    resolved = dict(lines)
    for line_ref, label in lines.items():
        if label == line_ref:
            human_name = await resolve_line_label(hass, line_ref)
            if human_name:
                resolved[line_ref] = human_name
    return resolved


async def fetch_stop_names(transit_company, requires_token, api_token):
    """Fetch the stop names and stop codes for the selected transit company."""
    transit_info = TRANSIT_COMPANIES.get(transit_company, {})
    stop_names = {}

    api_url = transit_info.get("api_url")
    endpoint = transit_info.get("endpoint")
    auth_type = transit_info.get("auth_type")

    if api_url and endpoint:
        url = f"{api_url}{endpoint}"
        headers = {}
        auth = None

        # Configurer l'authentification en fonction du type
        if auth_type == "Basic Auth":
            if requires_token and api_token:
                auth = aiohttp.BasicAuth(api_token, password='')
        elif auth_type == "Bearer Token":
            if requires_token and api_token:
                headers["Authorization"] = f"Bearer {api_token}"
        elif auth_type == "apiKey":
            if requires_token and api_token:
                headers["apiKey"] = api_token
        elif auth_type == "None":
            pass
        else:
            _LOGGER.error(f"Unknown auth_type: {auth_type}")

        _LOGGER.debug(f"Making API request to: {url}")
        _LOGGER.debug(f"Headers keys: {list(headers.keys())}")
        _LOGGER.debug(f"Auth configured: {auth is not None}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, auth=auth) as response:
                    _LOGGER.debug(f"Received response with status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        _LOGGER.debug(f"Response JSON: {data}")
                        stops = data.get("StopPointsDelivery", {}).get("AnnotatedStopPointRef", [])

                        # Collect stop names and associated codes
                        for stop in stops:
                            stop_name = stop.get("StopName")
                            stop_code = stop.get("Extension", {}).get("StopCode")
                            if stop_name:
                                if stop_name not in stop_names:
                                    stop_names[stop_name] = []
                                stop_names[stop_name].append(stop_code)
                    else:
                        _LOGGER.error(f"Failed to fetch stops: {response.status}")
        except aiohttp.ClientError as err:
            _LOGGER.error(f"HTTP error occurred: {err}")
    else:
        _LOGGER.warning("No stop list endpoint found for this company.")

    _LOGGER.debug(f"Fetched stop names and codes: {stop_names}")

    # Convert stop_names dictionary to a sorted list of tuples (stop_name, [stop_codes])
    sorted_stop_names = sorted(stop_names.items())
    return sorted_stop_names


async def _query_idfm_zones(where, limit):
    """Run a raw query against the public IDFM 'zones-d-arrets' referential.

    Shared by fetch_idfm_zones (search by name) and fetch_idfm_zones_by_zdcid
    (lookup pole siblings) — same public, unauthenticated dataset, only the filter
    differs.
    """
    params = {"where": where, "limit": limit}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(IDFM_ZONES_API_URL, params=params) as response:
                if response.status != 200:
                    _LOGGER.error(f"Failed to query IDFM zones: {response.status}")
                    return []
                data = await response.json()
    except aiohttp.ClientError as err:
        _LOGGER.error(f"HTTP error occurred while querying IDFM zones: {err}")
        return []

    return [
        {
            "zdaid": result["zdaid"],
            "zdaname": result["zdaname"],
            "zdatown": result.get("zdatown", ""),
            "zdatype": result.get("zdatype", ""),
            "zdcid": result.get("zdcid"),
        }
        for result in data.get("results", [])
    ]


async def fetch_idfm_zones(query):
    """Search the public IDFM 'zones-d-arrets' referential by stop name.

    Public, unauthenticated dataset. zdaid maps directly to the SIRI StopArea
    MonitoringRef (STIF:StopArea:SP:<zdaid>:) used by stop-monitoring — verified
    manually against STIF:StopArea:SP:45102: (Châtelet - Les Halles). This sidesteps
    stoppoints-discovery, which IDFM does not expose on the PRIM product used here
    (see the discovery_backend comment in const.py).
    """
    return await _query_idfm_zones(f'zdaname like "{query}"', 25)


async def fetch_idfm_zones_by_zdcid(zdcid):
    """List every zone sharing a correspondence pole (zdcid) — the sibling stops of
    a multimodal hub (ex. bus + metro platforms of the same place), for the pole
    opt-in step. PRIM-only: zdcid has no CTS equivalent.
    """
    return await _query_idfm_zones(f'zdcid="{zdcid}"', 25)
