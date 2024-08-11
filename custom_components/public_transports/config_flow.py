import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CITIES_DATA, SIRI_API_URLS

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

    async def async_step_user(self, user_input=None):
        """Handle the initial step where the user inputs a city name."""
        errors = {}

        if user_input is not None:
            self.city = user_input["city"]
            if self.city in CITIES_DATA:
                self.transit_companies = CITIES_DATA[self.city]
                return await self.async_step_select_company()
            else:
                errors["city"] = "city_not_found"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("city"): str}),
            description_placeholders={"city: Enter the name of your city (e.g. Paris)."},
            errors=errors,
        )

    async def async_step_select_company(self, user_input=None):
        """Handle the step where the user selects a transit company."""
        if user_input is not None:
            self.transit_company = user_input["transit_company"]
            # Check if the selected company uses SIRI-lite and if it requires a token
            if self.transit_company in SIRI_API_URLS:
                self.requires_token = SIRI_API_URLS[self.transit_company]["requires_token"]
                if self.requires_token:
                    return await self.async_step_get_token()
                else:
                    return await self.async_step_get_stop()

        options = {company: company for company in self.transit_companies}

        return self.async_show_form(
            step_id="select_company",
            data_schema=vol.Schema({vol.Required("transit_company"): vol.In(options)}),
            description_placeholders={"city: Enter the name of the public transports company (e.g. RATP)."},
        )

    async def async_step_get_token(self, user_input=None):
        """Handle the step where the user inputs an API token if required."""
        if user_input is not None:
            self.api_token = user_input["api_token"]
            return await self.async_step_get_stop()

        return self.async_show_form(
            step_id="get_token",
            data_schema=vol.Schema({vol.Required("api_token"): str}),
            description_placeholders={"city: Enter your secret token (e.g. a65d0a21-560c-43c7-a549-7a27e2413eef)."},            
        )

    async def async_step_get_stop(self, user_input=None):
        """Handle the step where the user inputs a stop name."""
        if user_input is not None:
            self.stop_name = user_input["stop_name"]
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
            description_placeholders={"stop_name: Enter the name of your stop"},  
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
