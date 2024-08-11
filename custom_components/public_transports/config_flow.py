import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import aiohttp
import asyncio
import logging
from .const import DOMAIN, CITIES_DATA, TRANSIT_COMPANIES

# Créez un logger spécifique pour votre intégration
_LOGGER = logging.getLogger(__name__)

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
        self.transit_companies = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step where the user inputs a city name."""
        errors = {}

        if user_input is not None:
            self.city = user_input.get("city")
            if self.city in CITIES_DATA:
                self.transit_companies = CITIES_DATA[self.city]
                return await self.async_step_select_company()
            else:
                errors["city"] = "La ville indiquée n'est pas valide."

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("city"): str}),
            description_placeholders={"city": "Entrez le nom de votre ville (ex. : Paris)."},
            errors=errors,
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
                errors = {"transit_company": "La compagnie de transport sélectionnée n'est pas valide."}
                return self.async_show_form(
                    step_id="select_company",
                    data_schema=vol.Schema({vol.Required("transit_company"): vol.In(self.transit_companies)}),
                    description_placeholders={"transit_company": "Choisissez la compagnie de transport."},
                    errors=errors
                )

        options = {company: company for company in self.transit_companies}

        return self.async_show_form(
            step_id="select_company",
            data_schema=vol.Schema({vol.Required("transit_company"): vol.In(options)}),
            description_placeholders={"transit_company": "Choisissez la compagnie de transport."},
        )

    async def async_step_get_token(self, user_input=None):
        """Handle the step where the user inputs an API token if required."""
        if user_input is not None:
            self.api_token = user_input.get("api_token")
            return await self.async_step_get_stop()

        return self.async_show_form(
            step_id="get_token",
            data_schema=vol.Schema({vol.Required("api_token"): str}),
            description_placeholders={"api_token": "Entrez votre token API (ex. : a65d0a21-560c-43c7-a549-7a27e2413eef)."},            
        )

    async def fetch_stop_names(self):
        """Fetch the stop names and stop codes for the selected transit company."""
        transit_info = TRANSIT_COMPANIES.get(self.transit_company, {})
        stop_names = []
        stop_codes = []

        api_url = transit_info.get("api_url")
        endpoint = transit_info.get("endpoint")

        if api_url and endpoint:
            url = f"{api_url}{endpoint}"
            headers = {}
            auth = None
            
            if self.requires_token and self.api_token:
                auth = aiohttp.BasicAuth(self.api_token, password='')

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
                            stop_names = [(stop.get("StopName"), stop.get("Extension", {}).get("StopCode")) for stop in stops]
                        else:
                            _LOGGER.error(f"Failed to fetch stops: {response.status}")
            except aiohttp.ClientError as err:
                _LOGGER.error(f"HTTP error occurred: {err}")
        else:
            _LOGGER.warning("No stop list endpoint found for this company.")
        
        _LOGGER.debug(f"Fetched stop names and codes: {stop_names}")

        return stop_names

    async def async_step_get_stop(self, user_input=None):
        """Handle the step where the user inputs a stop name."""
        errors = {}

        if user_input is not None:
            self.stop_name = user_input.get("stop_name")
            # Find the corresponding stop code
            selected_stop = next((stop for stop in self.stop_names if stop[0] == self.stop_name), None)
            if selected_stop:
                stop_code = selected_stop[1]
                return self.async_create_entry(
                    title=f"{self.city} - {self.transit_company}",
                    data={
                        "city": self.city,
                        "transit_company": self.transit_company,
                        "api_token": self.api_token,
                        "stop_name": self.stop_name,
                        "stop_code": stop_code
                    }
                )
            else:
                errors["stop_name"] = "L'arrêt sélectionné est invalide."

        # Simulate fetching stop names from an API or database
        self.stop_names = await self.fetch_stop_names()
        stop_names_list = [name for name, code in self.stop_names]

        if not stop_names_list:
            errors["stop_name"] = "Aucun arrêt de bus trouvé pour cette compagnie."

        return self.async_show_form(
            step_id="get_stop",
            data_schema=vol.Schema({vol.Required("stop_name"): vol.In(stop_names_list)}),
            description_placeholders={"stop_name": "Choisissez un arrêt de bus."},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Option flow for editing the configuration."""
        return PublicTransportsOptionsFlowHandler(config_entry)


class PublicTransportsOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return self.async_show_form(step_id="init")
