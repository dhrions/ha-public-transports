import logging

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, QUOTA_HUB_KIND
from .coordinator import (
    PublicTransportsDataUpdateCoordinator,
    entry_sense_specs,
    quota_key,
    spec_stop_codes,
)

_LOGGER = logging.getLogger(__name__)

# Cette intégration ne se configure QUE via config entries (UI), jamais par YAML — d'où
# le schéma « config entry only » exigé par hassfest dès qu'async_setup est défini.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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

    if entry.data.get("kind") != QUOTA_HUB_KIND:
        _ensure_quota_hub(hass, entry.data["transit_company"])

    return True


def _ensure_quota_hub(hass: HomeAssistant, transit_company: str) -> None:
    """Provision the (company, endpoint) quota-hub entry if none exists yet.

    Fire-and-forget (hass.async_create_task, never awaited here): triggering a config flow
    from inside another entry's own setup must not block on it. Safe against several stop
    entries of the same company starting up concurrently — async_step_integration_discovery
    aborts every attempt past the first via async_set_unique_id/_abort_if_unique_id_configured,
    so no lock is needed on this side.
    """
    existing = any(
        e.data.get("kind") == QUOTA_HUB_KIND and e.data.get("transit_company") == transit_company
        for e in hass.config_entries.async_entries(DOMAIN)
    )
    if existing:
        return
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={"transit_company": transit_company},
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Unload a config entry.

    Also detaches this entry's coordinators from the shared quota registry (cf.
    async_setup_entry). The quota/calls-today entities themselves live on a single,
    dedicated hub entry per company (cf. QUOTA_HUB_KIND) — unlike the arbitrary
    first-entry-wins ownership this replaced, there's no "who owns it" state to hand off
    here anymore.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_coordinators = hass.data[DOMAIN].pop(entry.entry_id, {})
        key = quota_key(entry.data["transit_company"])
        shared = hass.data[DOMAIN].get("_quota_coordinators", {}).get(key, [])
        for coordinator in entry_coordinators.values():
            if coordinator in shared:
                shared.remove(coordinator)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    """Cascade-remove a company's quota hub once its last stop entry is deleted.

    Called by HA while `entry` is still present in async_entries() (removed from the
    registry only after this hook returns) — siblings must therefore explicitly exclude
    entry.entry_id, not just filter by kind/company. A no-op for the hub entry itself: it
    owns no other entry, nothing to cascade from its own removal.
    """
    if entry.data.get("kind") == QUOTA_HUB_KIND:
        return
    company = entry.data.get("transit_company")
    siblings = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
        and e.data.get("kind") != QUOTA_HUB_KIND
        and e.data.get("transit_company") == company
    ]
    if siblings:
        return
    hub = next(
        (
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get("kind") == QUOTA_HUB_KIND and e.data.get("transit_company") == company
        ),
        None,
    )
    if hub:
        await hass.config_entries.async_remove(hub.entry_id)

