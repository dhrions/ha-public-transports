from homeassistant import config_entries
from homeassistant.core import HomeAssistant

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the public_transports component."""
    # @TODO: Add setup code.
    return True

async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Set up Public Transports from a config entry."""
    hass.data.setdefault("public_transports", {})

    # Vous pouvez stocker les données de l'entrée de configuration dans les données de l'intégration
    hass.data["public_transports"][entry.entry_id] = entry.data

    # Effectuez ici toute initialisation nécessaire pour votre intégration
    # Par exemple, créer des entités ou configurer des services
    # ...

    return True

