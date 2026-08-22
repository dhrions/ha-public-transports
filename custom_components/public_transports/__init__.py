import logging

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import (
    PublicTransportsDataUpdateCoordinator,
    entry_sense_specs,
    quota_key,
    spec_stop_codes,
)

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

    # v2 → v3 : les champs de filtre plats deviennent une liste "senses" (une entrée peut
    # désormais exposer 2 capteurs). entry_sense_specs lit déjà les 2 formes, mais on
    # matérialise la migration pour ne pas garder de champs plats ambigus.
    if entry.version < 3:
        specs = entry_sense_specs(entry)
        flat_keys = ("stop_code", "line_filter", "line_name", "direction_filter", "direction_label")
        data = {k: v for k, v in entry.data.items() if k not in flat_keys}
        data["senses"] = specs
        options = {k: v for k, v in entry.options.items() if k not in flat_keys}
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=3)
    return True

async def _async_update_listener(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> None:
    """Reload the entry when its options (line/direction filter) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Set up Public Transports from a config entry.

    One coordinator per distinct set of stop codes the entry needs (a "both senses" CTS
    entry has two codes → two coordinators; a PRIM entry has one; a "pole" PRIM entry
    merges several colocated codes into one). Sensors pick their coordinator by that same
    code set and apply their own filter.
    """
    hass.data.setdefault(DOMAIN, {})
    # Registre partagé, transverse aux entrées : la quantité d'appels/jour est plafonnée
    # par le producteur par (transporteur, endpoint) — cf. quota_key — pas par entrée. Deux
    # entrées PRIM sur le même jeton doivent donc alimenter le MÊME quota, pas deux quotas
    # indépendants qui liraient en double le même compteur côté producteur.
    quota_coordinators = hass.data[DOMAIN].setdefault("_quota_coordinators", {})

    code_sets = {
        tuple(spec_stop_codes(spec)) for spec in entry_sense_specs(entry) if spec_stop_codes(spec)
    }
    coordinators = {}
    for codes in code_sets:
        coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, list(codes))
        await coordinator.async_config_entry_first_refresh()
        coordinators[codes] = coordinator

    hass.data[DOMAIN][entry.entry_id] = coordinators
    key = quota_key(entry.data["transit_company"])
    quota_coordinators.setdefault(key, []).extend(coordinators.values())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Unload a config entry.

    Also detaches this entry's coordinators from the shared quota registry (cf.
    async_setup_entry) and drops the "who owns the quota entity for this key" marker if
    this entry was the owner — sensor.py's async_setup_entry reads that marker to decide
    whether to (re-)create the entity, so on this same entry's next setup (reload after an
    options change, or the user re-adding it) the quota entity is recreated fresh rather
    than silently missing. If a DIFFERENT entry still sharing the key never reloads
    afterward, its quota sensor stays "unavailable" (HA keeps the entity registered, it
    doesn't vanish) until something does reload it — a known, accepted limitation rather
    than a hidden bug (see quota_key's docstring).
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_coordinators = hass.data[DOMAIN].pop(entry.entry_id, {})
        key = quota_key(entry.data["transit_company"])
        shared = hass.data[DOMAIN].get("_quota_coordinators", {}).get(key, [])
        for coordinator in entry_coordinators.values():
            if coordinator in shared:
                shared.remove(coordinator)
        owners = hass.data[DOMAIN].get("_quota_owner_entry", {})
        if owners.get(key) == entry.entry_id:
            owners.pop(key, None)
            hass.data[DOMAIN].get("_quota_keys_with_entity", set()).discard(key)
    return unload_ok

