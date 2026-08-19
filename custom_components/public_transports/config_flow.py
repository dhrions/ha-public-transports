import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import aiohttp
import logging
from .const import DOMAIN, CITIES_DATA, TRANSIT_COMPANIES, IDFM_ZONES_API_URL, IDFM_LINES_API_URL
from .coordinator import build_siri_client, scalar

# Créez un logger spécifique pour votre intégration
_LOGGER = logging.getLogger(__name__)

# Sentinelles « pas de filtre » pour les listes déroulantes ligne / sens.
ALL_LINES = "__all__"
ALL_DIRECTIONS = "__all__"

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
    """Distinct destination names, optionally restricted to one line."""
    return sorted({
        scalar(call.destination_name)
        for call in calls
        if scalar(call.destination_name) and (not line_ref or scalar(call.line_ref) == line_ref)
    })


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

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the config flow."""
        self.city = None
        self.transit_company = None
        self.requires_token = False
        self.api_token = None
        self.stop_name = None
        self.stop_code = None
        self.transit_companies = []
        self.zone_matches = []
        self.available_calls = []
        self.line_filter = None
        self.line_name = None
        self.direction_filter = None

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
            data_schema=vol.Schema({vol.Required("city"): vol.In(available_cities)}),
        )

    async def async_step_select_company(self, user_input=None):
        """Handle the step where the user selects a transit company."""
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
                    data_schema=vol.Schema({vol.Required("transit_company"): vol.In(self.transit_companies)}),
                    errors=errors
                )

        options = {company: company for company in self.transit_companies}

        return self.async_show_form(
            step_id="select_company",
            data_schema=vol.Schema({vol.Required("transit_company"): vol.In(options)}),
        )

    async def async_step_get_token(self, user_input=None):
        """Handle the step where the user inputs an API token if required."""
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

        if user_input is not None:
            self.stop_name = user_input.get("stop_name")
            # Find the corresponding stop codes
            selected_stop = next((stop for stop in self.stop_names if stop[0] == self.stop_name), None)
            if selected_stop:
                _, stop_codes = selected_stop
                # Use the first stop code for the entry
                self.stop_code = stop_codes[0] if stop_codes else None
                return await self.async_step_filters()
            else:
                errors["stop_name"] = "invalid_stop"

        # Simulate fetching stop names from an API or database
        self.stop_names = await self.fetch_stop_names()
        stop_names_list = [name for name, codes in self.stop_names]

        if not stop_names_list:
            errors["stop_name"] = "no_stops_found"

        return self.async_show_form(
            step_id="get_stop",
            data_schema=vol.Schema({vol.Required("stop_name"): vol.In(stop_names_list)}),
            errors=errors,
        )

    async def fetch_idfm_zones(self, query):
        """Search the public IDFM 'zones-d-arrets' referential by stop name.

        Public, unauthenticated dataset. zdaid maps directly to the SIRI StopArea
        MonitoringRef (STIF:StopArea:SP:<zdaid>:) used by stop-monitoring — verified
        manually against STIF:StopArea:SP:45102: (Châtelet - Les Halles). This sidesteps
        stoppoints-discovery, which IDFM does not expose on the PRIM product used here
        (see the discovery_backend comment in const.py).
        """
        params = {"where": f'zdaname like "{query}"', "limit": 25}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(IDFM_ZONES_API_URL, params=params) as response:
                    if response.status != 200:
                        _LOGGER.error(f"Failed to search IDFM zones: {response.status}")
                        return []
                    data = await response.json()
        except aiohttp.ClientError as err:
            _LOGGER.error(f"HTTP error occurred while searching IDFM zones: {err}")
            return []

        return [
            {
                "zdaid": result["zdaid"],
                "zdaname": result["zdaname"],
                "zdatown": result.get("zdatown", ""),
                "zdatype": result.get("zdatype", ""),
            }
            for result in data.get("results", [])
        ]

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
                return await self.async_step_filters()
            errors["stop_id"] = "invalid_stop"

        return self.async_show_form(
            step_id="get_stop_select",
            data_schema=vol.Schema({vol.Required("stop_id"): vol.In(options)}),
            errors=errors,
        )

    async def async_step_filters(self, user_input=None):
        """Probe the stop and route to line selection, or manual entry if nothing runs."""
        self.available_calls = await probe_available_passages(
            self.hass, self.transit_company, self.api_token, self.stop_code
        )
        if not self.available_calls:
            return await self.async_step_filters_manual()
        return await self.async_step_select_line()

    async def async_step_select_line(self, user_input=None):
        """Let the user optionally restrict the stop to a single line."""
        lines = await resolve_line_names(self.hass, _lines_from_calls(self.available_calls))
        options = {ALL_LINES: "Toutes les lignes", **lines}

        if user_input is not None:
            choice = user_input.get("line")
            if choice and choice != ALL_LINES:
                self.line_filter = choice
                self.line_name = lines.get(choice)
            else:
                self.line_filter = None
                self.line_name = None
            return await self.async_step_select_direction()

        return self.async_show_form(
            step_id="select_line",
            data_schema=vol.Schema({vol.Required("line", default=ALL_LINES): vol.In(options)}),
        )

    async def async_step_select_direction(self, user_input=None):
        """Let the user optionally restrict the stop to a single direction (terminus)."""
        directions = _directions_from_calls(self.available_calls, self.line_filter)
        options = {ALL_DIRECTIONS: "Tous les sens", **{d: d for d in directions}}

        if user_input is not None:
            choice = user_input.get("direction")
            self.direction_filter = None if not choice or choice == ALL_DIRECTIONS else choice
            return self._create_entry()

        return self.async_show_form(
            step_id="select_direction",
            data_schema=vol.Schema({vol.Required("direction", default=ALL_DIRECTIONS): vol.In(options)}),
        )

    async def async_step_filters_manual(self, user_input=None):
        """Fallback when nothing is circulating: type an optional line/direction."""
        if user_input is not None:
            line = (user_input.get("line") or "").strip()
            direction = (user_input.get("direction") or "").strip()
            self.line_filter = line or None
            self.line_name = line or None
            self.direction_filter = direction or None
            return self._create_entry()

        return self.async_show_form(
            step_id="filters_manual",
            data_schema=vol.Schema({
                vol.Optional("line", default=""): str,
                vol.Optional("direction", default=""): str,
            }),
        )

    def _create_entry(self):
        """Create the config entry with the stop and optional line/direction filters."""
        return self.async_create_entry(
            title=f"{self.city} - {self.transit_company}",
            data={
                "city": self.city,
                "transit_company": self.transit_company,
                "api_token": self.api_token,
                "stop_name": self.stop_name,
                "stop_code": self.stop_code,
                "line_filter": self.line_filter,
                "line_name": self.line_name,
                "direction_filter": self.direction_filter,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Option flow for editing the line/direction filter."""
        return PublicTransportsOptionsFlowHandler(config_entry)


class PublicTransportsOptionsFlowHandler(config_entries.OptionsFlow):
    """Let the user re-choose the line/direction filter after setup."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Re-probe the stop and edit the line/direction filter."""
        data = self.config_entry.data
        current = {**data, **self.config_entry.options}

        calls = await probe_available_passages(
            self.hass, data["transit_company"], data.get("api_token"), data["stop_code"]
        )
        lines = await resolve_line_names(self.hass, _lines_from_calls(calls))
        line_options = {ALL_LINES: "Toutes les lignes", **lines}
        dir_options = {ALL_DIRECTIONS: "Tous les sens", **{d: d for d in _directions_from_calls(calls)}}

        # Toujours proposer le filtre courant, même si cette ligne/ce sens ne circule pas
        # au moment du re-sondage (sinon vol.In rejetterait la valeur par défaut).
        cur_line = current.get("line_filter") or ALL_LINES
        cur_dir = current.get("direction_filter") or ALL_DIRECTIONS
        if cur_line != ALL_LINES and cur_line not in line_options:
            line_options[cur_line] = current.get("line_name") or cur_line
        if cur_dir != ALL_DIRECTIONS and cur_dir not in dir_options:
            dir_options[cur_dir] = cur_dir

        if user_input is not None:
            line = user_input.get("line")
            direction = user_input.get("direction")
            line_filter = None if not line or line == ALL_LINES else line
            direction_filter = None if not direction or direction == ALL_DIRECTIONS else direction
            return self.async_create_entry(
                title="",
                data={
                    "line_filter": line_filter,
                    "line_name": line_options.get(line) if line_filter else None,
                    "direction_filter": direction_filter,
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("line", default=cur_line): vol.In(line_options),
                vol.Required("direction", default=cur_dir): vol.In(dir_options),
            }),
        )
