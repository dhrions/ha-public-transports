import logging

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import PublicTransportsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the public_transports component."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Migrate old config entries.

    v1 → v2 : le filtre de sens portait un terminus (DestinationName) ; il porte
    désormais un DirectionRef (Aller/Retour). L'ancienne valeur est incompatible avec le
    nouveau filtrage — on l'efface (retour à « tous les sens ») plutôt que de risquer un
    capteur vide. L'utilisateur re-choisit son sens via les options si besoin.
    """
    if entry.version < 2:
        data = {k: v for k, v in entry.data.items() if k not in ("direction_filter", "direction_label")}
        options = {k: v for k, v in entry.options.items() if k not in ("direction_filter", "direction_label")}
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)
    return True

async def _async_update_listener(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> None:
    """Reload the entry when its options (line/direction filter) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Set up Public Transports from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

