import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CITIES_DATA, TRANSIT_COMPANIES

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

    async def async_step_get_stop(self, user_input=None):
        """Handle the step where the user inputs a stop name."""
        if user_input is not None:
            self.stop_name = user_input.get("stop_name")
            return self.async_create_entry(
                title=f"{self.city} - {self.transit_company}",
                data={
                    "city": self.city,
                    "transit_company": self.transit_company,
                    "api_token": self.api_token,
                    "stop_name": self.stop_name
                }
            )

        return self.async_show_form(
            step_id="get_stop",
            data_schema=vol.Schema({vol.Required("stop_name"): str}),
            description_placeholders={"stop_name": "Entrez le nom de l'arrêt de bus (ex. : Gare, Centre-ville)."},  
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
