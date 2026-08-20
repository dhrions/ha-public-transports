import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CITIES_DATA,
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_SCAN_INTERVAL,
    DEFAULT_ACTIVE_END,
    DEFAULT_ACTIVE_START,
    DOMAIN,
    IDFM_LINES_API_URL,
    IDFM_ZONES_API_URL,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    TRANSIT_COMPANIES,
)
from .coordinator import (
    build_siri_client,
    entry_scan_interval,
    entry_sense_specs,
    scalar,
)


def dropdown(options):
    """Build a searchable dropdown selector from an {value: label} mapping.

    Replaces vol.In(...) so long lists (cities, hundreds of CTS stops) get a search box
    instead of a radio list. Labels are passed inline, sidestepping translation of dynamic
    values (stop/line names).
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=str(value), label=str(label))
                for value, label in options.items()
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )

# Créez un logger spécifique pour votre intégration
_LOGGER = logging.getLogger(__name__)

# Sentinelles « pas de filtre » pour les listes déroulantes ligne / sens.
ALL_LINES = "__all__"
ALL_DIRECTIONS = "__all__"
# Sentinelle « les deux sens » : crée une entrée qui expose 2 capteurs (un par sens).
BOTH_SENSES = "__both__"
# Sentinelle « une ligne par capteur » : crée une entrée qui expose un capteur par ligne
# détectée à l'arrêt (sens fusionnés), pour un pôle multimodal (métro + bus + tram...).
SPLIT_LINES = "__split__"

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
    config flow sees exactly the passages the sensor will. Returns [] on any error or when
    nothing is currently circulating.
    """
    transit_info = TRANSIT_COMPANIES.get(transit_company, {})
    client = build_siri_client(transit_info, api_token, stop_code)
    try:
        return await hass.async_add_executor_job(client.fetch_next_calls)
    except Exception as err:  # noqa: BLE001 - config flow must not crash on probe failure
        _LOGGER.error(f"Error probing stop for lines/directions: {err}")
        return []


def _lines_from_calls(calls):
    """Distinct {line_ref: published_line_name} from a list of MonitoredCall."""
    lines = {}
    for call in calls:
        line_ref = scalar(call.line_ref)
        if line_ref:
            lines[line_ref] = scalar(call.published_line_name) or line_ref
    return lines


def _directions_from_calls(calls, line_ref=None):
    """Group passages by real sense (SIRI DirectionRef), optionally within one line.

    Returns {direction_ref: label} where label is composed from the distinct terminuses
    seen for that sense (ex. "Asnières… / Saint-Denis…" vs "Châtillon Montrouge"). This is
    the 2-way sense the user picks — the per-vehicle terminus stays for the sensor display.
    A forked line (ex. metro 13) has several terminuses per sense, hence the join.
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
    return {
        direction_ref: " / ".join(sorted(terminuses)) or direction_ref
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


class PublicTransportsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Public Transports."""

    VERSION = 3
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the config flow."""
        self.city = None
        self.transit_company = None
        self.requires_token = False
        self.api_token = None
        self.stop_name = None
        self.stop_code = None
        self.candidate_codes = []
        self.transit_companies = []
        self.zone_matches = []
        self.available_calls = []
        self.candidate_calls = []
        self.line_filter = None
        self.line_name = None
        self.direction_filter = None
        self.direction_label = None
        self.senses = []
        self.pole_codes = None
        self.pole_zones = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step where the user inputs a city name."""
        available_cities = sorted(
            city
            for city, companies in CITIES_DATA.items()
            if any(company in TRANSIT_COMPANIES for company in companies)
        )

        if user_input is not None:
            self.city = user_input.get("city")
            self.transit_companies = [
                company for company in CITIES_DATA[self.city] if company in TRANSIT_COMPANIES
            ]
            return await self.async_step_select_company()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("city"): dropdown({c: c for c in available_cities})
            }),
        )

    async def async_step_select_company(self, user_input=None):
        """Handle the step where the user selects a transit company.

        Skipped (auto-selected) when the city has only one company — no real choice.
        """
        if len(self.transit_companies) == 1 and user_input is None:
            user_input = {"transit_company": self.transit_companies[0]}

        if user_input is not None:
            self.transit_company = user_input.get("transit_company")
            if self.transit_company in TRANSIT_COMPANIES:
                transit_info = TRANSIT_COMPANIES[self.transit_company]
                self.requires_token = transit_info["requires_token"]
                if self.requires_token:
                    return await self.async_step_get_token()
                else:
                    return await self.async_step_get_stop()
            else:
                errors = {"transit_company": "invalid_company"}
                return self.async_show_form(
                    step_id="select_company",
                    data_schema=vol.Schema({vol.Required("transit_company"): dropdown({c: c for c in self.transit_companies})}),
                    errors=errors
                )

        options = {company: company for company in self.transit_companies}

        return self.async_show_form(
            step_id="select_company",
            data_schema=vol.Schema({vol.Required("transit_company"): dropdown(options)}),
        )

    def _reused_token(self):
        """Return an api_token already entered for the same company, if any.

        Lets the flow skip the token step when the user adds another stop for a company
        they already configured — "ne pas redemander le token".
        """
        for entry in self._async_current_entries():
            if entry.data.get("transit_company") == self.transit_company and entry.data.get("api_token"):
                return entry.data["api_token"]
        return None

    async def async_step_get_token(self, user_input=None):
        """Handle the step where the user inputs an API token if required."""
        if user_input is None:
            reused = self._reused_token()
            if reused:
                self.api_token = reused
                return await self.async_step_get_stop()

        if user_input is not None:
            self.api_token = user_input.get("api_token")
            return await self.async_step_get_stop()

        return self.async_show_form(
            step_id="get_token",
            data_schema=vol.Schema({vol.Required("api_token"): str}),
        )

    async def fetch_stop_names(self):
        """Fetch the stop names and stop codes for the selected transit company."""
        transit_info = TRANSIT_COMPANIES.get(self.transit_company, {})
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
                if self.requires_token and self.api_token:
                    auth = aiohttp.BasicAuth(self.api_token, password='')
            elif auth_type == "Bearer Token":
                if self.requires_token and self.api_token:
                    headers["Authorization"] = f"Bearer {self.api_token}"
            elif auth_type == "apiKey":
                if self.requires_token and self.api_token:
                    headers["apiKey"] = self.api_token
            elif auth_type == "None":
                pass
            else:
                _LOGGER.error(f"Unknown auth_type: {auth_type}")

            _LOGGER.debug(f"Making API request to: {url}")
            _LOGGER.debug(f"Headers: {headers}")
            _LOGGER.debug(f"Auth: {auth}")

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

    async def async_step_get_stop(self, user_input=None):
        """Handle the step where the user inputs a stop name."""
        transit_info = TRANSIT_COMPANIES.get(self.transit_company, {})
        if transit_info.get("discovery_backend") == "idfm_zones":
            return await self.async_step_get_stop_search(user_input)

        errors = {}

        # Un même nom CTS recouvre souvent plusieurs codes d'arrêt distincts, un par
        # sens/quai (ex. "Barr" -> 43A ET 43B). On ne choisit pas le code ici : tous les
        # candidats sont sondés (async_step_filters), et le bon code sera fixé
        # automatiquement une fois le sens choisi (async_step_select_direction).
        self.stop_names = await self.fetch_stop_names()
        stop_options = {name: name for name, codes in self.stop_names}

        if user_input is not None:
            choice = user_input.get("stop_name")
            codes = dict(self.stop_names).get(choice)
            if codes:
                self.stop_name = choice
                self.candidate_codes = codes
                return await self.async_step_filters()
            errors["stop_name"] = "invalid_stop"

        if not stop_options:
            errors["stop_name"] = "no_stops_found"

        return self.async_show_form(
            step_id="get_stop",
            data_schema=vol.Schema({vol.Required("stop_name"): dropdown(stop_options)}),
            errors=errors,
        )

    async def _query_idfm_zones(self, where, limit):
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

    async def fetch_idfm_zones(self, query):
        """Search the public IDFM 'zones-d-arrets' referential by stop name.

        Public, unauthenticated dataset. zdaid maps directly to the SIRI StopArea
        MonitoringRef (STIF:StopArea:SP:<zdaid>:) used by stop-monitoring — verified
        manually against STIF:StopArea:SP:45102: (Châtelet - Les Halles). This sidesteps
        stoppoints-discovery, which IDFM does not expose on the PRIM product used here
        (see the discovery_backend comment in const.py).
        """
        return await self._query_idfm_zones(f'zdaname like "{query}"', 25)

    async def fetch_idfm_zones_by_zdcid(self, zdcid):
        """List every zone sharing a correspondence pole (zdcid) — the sibling stops of
        a multimodal hub (ex. bus + metro platforms of the same place), for the pole
        opt-in step. PRIM-only: zdcid has no CTS equivalent.
        """
        return await self._query_idfm_zones(f'zdcid="{zdcid}"', 25)

    async def async_step_get_stop_search(self, user_input=None):
        """Ask for a stop name to search, for companies using the idfm_zones backend."""
        errors = {}

        if user_input is not None:
            query = user_input.get("query", "")
            self.zone_matches = await self.fetch_idfm_zones(query)
            if self.zone_matches:
                return await self.async_step_get_stop_select()
            errors["query"] = "no_search_results"

        return self.async_show_form(
            step_id="get_stop_search",
            data_schema=vol.Schema({vol.Required("query"): str}),
            errors=errors,
        )

    async def async_step_get_stop_select(self, user_input=None):
        """Handle stop selection among the search results found via idfm_zones."""
        errors = {}
        def label(zone):
            parts = [zone["zdaname"]]
            if zone["zdatown"]:
                parts.append(f"— {zone['zdatown']}")
            mode = ZDATYPE_LABELS.get(zone["zdatype"], zone["zdatype"])
            if mode:
                parts.append(f"({mode})")
            return " ".join(parts)

        options = {
            zone["zdaid"]: label(zone)
            for zone in self.zone_matches
        }

        if user_input is not None:
            zdaid = user_input.get("stop_id")
            zone = next((z for z in self.zone_matches if z["zdaid"] == zdaid), None)
            if zone:
                self.stop_name = zone["zdaname"]
                self.stop_code = f"STIF:StopArea:SP:{zdaid}:"
                self.candidate_codes = [self.stop_code]

                zdcid = zone.get("zdcid")
                if zdcid:
                    siblings = await self.fetch_idfm_zones_by_zdcid(zdcid)
                    self.pole_zones = [z for z in siblings if z["zdaid"] != zdaid]
                    if self.pole_zones:
                        return await self.async_step_pole_confirm()

                return await self.async_step_filters()
            errors["stop_id"] = "invalid_stop"

        return self.async_show_form(
            step_id="get_stop_select",
            data_schema=vol.Schema({vol.Required("stop_id"): dropdown(options)}),
            errors=errors,
        )

    async def async_step_pole_confirm(self, user_input=None):
        """Offer to merge this stop with the sibling stops of its multimodal hub
        (same IDFM zdcid — ex. bus + metro platforms of the same place) into one sensor,
        instead of tracking this single physical stop.
        """
        POLE_YES = "__pole_yes__"
        POLE_NO = "__pole_no__"

        if user_input is not None:
            if user_input.get("pole") == POLE_YES:
                sibling_codes = [f"STIF:StopArea:SP:{z['zdaid']}:" for z in self.pole_zones]
                self.pole_codes = [self.stop_code] + sibling_codes
                self.candidate_codes = self.pole_codes
            return await self.async_step_filters()

        names = ", ".join(z["zdaname"] for z in self.pole_zones)
        options = {
            POLE_YES: f"Oui, regrouper les {len(self.pole_zones) + 1} arrêts du pôle ({names})",
            POLE_NO: "Non, cet arrêt seul",
        }
        return self.async_show_form(
            step_id="pole_confirm",
            data_schema=vol.Schema({vol.Required("pole", default=POLE_NO): dropdown(options)}),
        )

    async def async_step_filters(self, user_input=None):
        """Probe every candidate stop code and route to line selection.

        A stop name can cover several physical codes (ex. CTS "Barr" -> 43A/43B, one per
        sense). All are probed and merged here so line/direction selection sees the full
        picture; async_step_select_direction picks the right code once the sense is chosen.
        """
        self.candidate_calls = []
        for code in self.candidate_codes:
            calls = await probe_available_passages(
                self.hass, self.transit_company, self.api_token, code
            )
            self.candidate_calls.extend((call, code) for call in calls)
        self.available_calls = [call for call, _ in self.candidate_calls]

        if not self.available_calls:
            return await self.async_step_filters_manual()
        return await self.async_step_select_line()

    async def async_step_select_line(self, user_input=None):
        """Let the user optionally restrict the stop to a single line.

        Skipped (auto-selected) when at most one line is actually circulating — a choice
        between "cette ligne" et "toutes les lignes" ne veut rien dire s'il n'y en a
        qu'une.
        """
        lines = await resolve_line_names(self.hass, _lines_from_calls(self.available_calls))

        if len(lines) <= 1:
            if lines:
                self.line_filter, self.line_name = next(iter(lines.items()))
            else:
                self.line_filter = None
                self.line_name = None
            return await self.async_step_select_direction()

        # Pas de "toutes les lignes" séparé : redondant avec "une ligne par capteur"
        # (même résultat — tout suivre — juste fusionné en 1 capteur au lieu de N), même
        # logique que le retrait de "tous les sens" au profit de "les deux sens".
        options = dict(lines)
        # "Une ligne par capteur" n'a de sens que si on peut fixer un stop_code unique —
        # sur un arrêt CTS ambigu (plusieurs codes physiques), un capteur par ligne
        # nécessiterait de lire plusieurs coordinators, pas supporté aujourd'hui.
        splittable = len(self.candidate_codes) <= 1
        if splittable:
            options[SPLIT_LINES] = "Une ligne par capteur (toutes les lignes)"

        if user_input is not None:
            choice = user_input.get("line")
            if splittable and choice == SPLIT_LINES:
                code = self.stop_code or (sorted(self.candidate_codes)[0] if self.candidate_codes else None)
                self.senses = []
                for ref, name in lines.items():
                    self.line_filter, self.line_name = ref, name
                    senses_for_line = _directions_from_calls(self.available_calls, ref)
                    if len(senses_for_line) <= 1:
                        # Une seule spec pour cette ligne (sens fusionné) — même repli
                        # que partout ailleurs quand il n'y a qu'un choix réel.
                        d_filter, d_label = next(iter(senses_for_line.items())) if senses_for_line else (None, None)
                        self.senses.append(self._spec(code, d_filter, d_label))
                    else:
                        # Une spec par sens réel de cette ligne (ex. 2 sens RER par ligne).
                        self.senses.extend(
                            self._spec(code, d_filter, d_label)
                            for d_filter, d_label in senses_for_line.items()
                        )
                return self._create_entry()
            self.line_filter = choice
            self.line_name = lines.get(choice)
            return await self.async_step_select_direction()

        return self.async_show_form(
            step_id="select_line",
            data_schema=vol.Schema({vol.Required("line", default=next(iter(options))): dropdown(options)}),
        )

    def _codes_from_candidates(self):
        """One entry per physical candidate code — the structural sense split for an
        ambiguous CTS stop (ex. "Barr" -> 43A/43B).

        Unlike _directions_from_calls (which groups by the live DirectionRef of whatever
        is currently circulating), this always lists every candidate code, even one with
        zero passages during the probe (a transient traffic gap on one platform is not the
        absence of that sense). The label is composed from that code's own probed
        terminuses when available, or the bare code as a last resort.
        """
        options = {}
        for code in self.candidate_codes:
            code_calls = [
                call for call, c in self.candidate_calls
                if c == code and (not self.line_filter or scalar(call.line_ref) == self.line_filter)
            ]
            terminuses = sorted({
                scalar(call.destination_name) for call in code_calls if scalar(call.destination_name)
            })
            options[code] = " / ".join(terminuses) if terminuses else code
        return options

    def _spec(self, stop_code=None, direction_filter=None, direction_label=None, stop_codes=None):
        """Build one sense spec (= one future sensor) from the current flow state.

        stop_code falls back to the single candidate when not given explicitly — needed
        for a non-ambiguous CTS stop (one code), where self.stop_code is never assigned
        (only PRIM and the ambiguous-CTS branch set it directly). Without this fallback
        the spec silently got stop_code=None, and no sensor was ever created for it.

        stop_codes (pole mode only) additionally lists every physical code the sensor
        should merge — stop_code stays the primary/first one for display/back-compat.
        """
        stop_code = stop_code or (sorted(self.candidate_codes)[0] if self.candidate_codes else None)
        spec = {
            "stop_code": stop_code,
            "line_filter": self.line_filter,
            "line_name": self.line_name,
            "direction_filter": direction_filter,
            "direction_label": direction_label,
        }
        if stop_codes:
            spec["stop_codes"] = stop_codes
        return spec

    async def async_step_select_direction(self, user_input=None):
        """Let the user pick a sense — one, or both (2 sensors).

        Ambiguous CTS stop (several physical codes) : chaque code EST un sens (un capteur
        ne suit qu'un seul MonitoringRef), toujours proposé même sans trafic instantané sur
        l'un des deux (cf. _codes_from_candidates). Un sens -> 1 spec sur ce code ; « les
        deux sens » -> une spec par code.

        Sinon (un seul code physique, ex. PRIM) : sens dérivés du DirectionRef des passages
        en circulation. Un sens -> 1 spec avec ce direction_filter ; « tous les sens » ->
        1 spec sans filtre (un seul capteur) ; « les deux sens » -> une spec par sens.
        """
        if len(self.candidate_codes) > 1 and not self.pole_codes:
            codes = self._codes_from_candidates()

            if len(codes) <= 1:
                code, label = next(iter(codes.items())) if codes else (sorted(self.candidate_codes)[0], None)
                self.senses = [self._spec(code, direction_label=label)]
                return self._create_entry()

            options = {**codes, BOTH_SENSES: "Les deux sens (2 capteurs)"}

            if user_input is not None:
                choice = user_input.get("direction")
                if choice == BOTH_SENSES:
                    self.senses = [self._spec(code, direction_label=label) for code, label in codes.items()]
                else:
                    self.senses = [self._spec(choice, direction_label=codes.get(choice))]
                return self._create_entry()

            return self.async_show_form(
                step_id="select_direction",
                data_schema=vol.Schema({vol.Required("direction", default=next(iter(codes))): dropdown(options)}),
            )

        senses = _directions_from_calls(self.available_calls, self.line_filter)

        if len(senses) <= 1:
            direction_filter, direction_label = next(iter(senses.items())) if senses else (None, None)
            self.senses = [self._spec(self.stop_code, direction_filter, direction_label, self.pole_codes)]
            return self._create_entry()

        # Pas de "tous les sens" séparé : ça reviendrait au même que "les deux sens" (tout
        # suivre), juste avec un seul capteur fusionné au lieu de deux — redondant.
        options = {**senses, BOTH_SENSES: "Les deux sens (2 capteurs)"}

        if user_input is not None:
            choice = user_input.get("direction")
            if choice == BOTH_SENSES:
                self.senses = [self._spec(self.stop_code, d, label, self.pole_codes) for d, label in senses.items()]
            else:
                self.senses = [self._spec(self.stop_code, choice, senses.get(choice), self.pole_codes)]
            return self._create_entry()

        return self.async_show_form(
            step_id="select_direction",
            data_schema=vol.Schema({vol.Required("direction", default=next(iter(senses))): dropdown(options)}),
        )

    async def async_step_filters_manual(self, user_input=None):
        """Fallback when nothing is circulating: type an optional line.

        Le sens (DirectionRef = Aller/Retour) n'est pas saisissable utilement à la main —
        il se choisira via Options une fois des passages visibles.
        """
        if user_input is not None:
            line = (user_input.get("line") or "").strip()
            self.line_filter = line or None
            self.line_name = line or None
            code = self.stop_code or (sorted(self.candidate_codes)[0] if self.candidate_codes else None)
            self.senses = [self._spec(code, stop_codes=self.pole_codes)]
            return self._create_entry()

        return self.async_show_form(
            step_id="filters_manual",
            data_schema=vol.Schema({
                vol.Optional("line", default=""): str,
            }),
        )

    def _create_entry(self):
        """Create the config entry from the accumulated sense specs (1 or 2 sensors)."""
        return self.async_create_entry(
            title=f"{self.city} - {self.transit_company}",
            data={
                "city": self.city,
                "transit_company": self.transit_company,
                "api_token": self.api_token,
                "stop_name": self.stop_name,
                "senses": self.senses,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Option flow for editing the line/direction filter."""
        return PublicTransportsOptionsFlowHandler(config_entry)


class PublicTransportsOptionsFlowHandler(config_entries.OptionsFlow):
    """Let the user re-choose the line/direction filter after setup.

    Mono-sens entry : ligne + sens éditables. Entrée « 2 sens » (2 specs) : seule la ligne
    est éditable (appliquée aux 2 sens) — le partage en 2 sens reste tel quel.
    """

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Re-probe the stop and edit the line (and, mono-sens, the direction) filter."""
        data = self.config_entry.data
        specs = entry_sense_specs(self.config_entry)
        primary = specs[0]
        multi_sense = len(specs) > 1

        calls = await probe_available_passages(
            self.hass, data["transit_company"], data.get("api_token"), primary.get("stop_code")
        )
        lines = await resolve_line_names(self.hass, _lines_from_calls(calls))
        line_options = {ALL_LINES: "Toutes les lignes", **lines}
        cur_line = primary.get("line_filter") or ALL_LINES
        if cur_line != ALL_LINES and cur_line not in line_options:
            line_options[cur_line] = primary.get("line_name") or cur_line

        senses = _directions_from_calls(calls, primary.get("line_filter"))
        dir_options = {ALL_DIRECTIONS: "Tous les sens", **senses}
        cur_dir = primary.get("direction_filter") or ALL_DIRECTIONS
        if cur_dir != ALL_DIRECTIONS and cur_dir not in dir_options:
            dir_options[cur_dir] = primary.get("direction_label") or cur_dir

        config = {**self.config_entry.data, **self.config_entry.options}
        cur_interval = int(
            entry_scan_interval(self.config_entry).total_seconds() // 60
        )
        cur_start = config.get(CONF_ACTIVE_START) or DEFAULT_ACTIVE_START
        cur_end = config.get(CONF_ACTIVE_END) or DEFAULT_ACTIVE_END

        if user_input is not None:
            line = user_input.get("line")
            line_filter = None if not line or line == ALL_LINES else line
            line_name = line_options.get(line) if line_filter else None
            if multi_sense:
                # ligne appliquée aux 2 specs, sens conservés
                new_specs = [
                    {**spec, "line_filter": line_filter, "line_name": line_name}
                    for spec in specs
                ]
            else:
                direction = user_input.get("direction")
                direction_filter = None if not direction or direction == ALL_DIRECTIONS else direction
                new_specs = [{
                    **primary,
                    "line_filter": line_filter,
                    "line_name": line_name,
                    "direction_filter": direction_filter,
                    "direction_label": dir_options.get(direction) if direction_filter else None,
                }]
            return self.async_create_entry(title="", data={
                "senses": new_specs,
                CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, cur_interval),
                CONF_ACTIVE_START: user_input.get(CONF_ACTIVE_START, cur_start),
                CONF_ACTIVE_END: user_input.get(CONF_ACTIVE_END, cur_end),
            })

        schema = {vol.Required("line", default=cur_line): dropdown(line_options)}
        if not multi_sense:
            schema[vol.Required("direction", default=cur_dir)] = dropdown(dir_options)
        # Fréquence et plage active : ce qui décide de la consommation de quota, un appel
        # étant émis par code d'arrêt et par cycle (cf. const.py).
        schema[vol.Required(CONF_SCAN_INTERVAL, default=cur_interval)] = vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES),
        )
        schema[vol.Required(CONF_ACTIVE_START, default=cur_start)] = str
        schema[vol.Required(CONF_ACTIVE_END, default=cur_end)] = str
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
