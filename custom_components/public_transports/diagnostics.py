"""Diagnostics support for Public Transports.

Motivé par l'inaccessibilité de l'instance de test (vivid-yam) : ni SSH, ni
filesystem, ni `.storage` lisibles à distance (cf. DEVELOPPEMENT.md). Ce module
expose, via *Télécharger les diagnostics* de l'UI, ce que ni `/api/states` ni
`/api/template` ne donnent : les specs de sens réellement enregistrées dans
l'entrée (line_filter/direction_filter figés à la création) confrontées au flux
brut que chaque coordinator reçoit de l'API — la seule vue qui permet de dire si
un capteur « voit la mauvaise ligne » à cause de sa spec ou du filtrage runtime.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import scalar

# api_token est le seul secret de l'entrée ; jamais dans le dump. stop_code(s) et
# les noms de ligne/sens sont des identifiants publics de réseau, pas sensibles.
TO_REDACT = {"api_token"}


def _call_summary(call) -> dict[str, Any]:
    """Réduit un MonitoredCall brut aux champs qui servent au diagnostic de filtrage."""
    return {
        "line_ref": scalar(call.line_ref),
        "published_line_name": scalar(call.published_line_name),
        "direction_ref": scalar(call.direction_ref),
        "destination_name": scalar(call.destination_name),
        "expected_arrival_time": call.expected_arrival_time,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Deux volets complémentaires :
    - `entry` : les données figées de l'entrée (dont `senses`), token caviardé —
      pour lire les filtres réellement enregistrés.
    - `coordinators` : l'état live de chaque coordinator (codes physiques, succès
      du dernier refresh, quota, compteur d'appels) + un échantillon du flux brut
      *non filtré* qu'il a reçu — pour confronter « ce que l'API renvoie » à « ce
      que chaque spec prétend suivre ».
    """
    coordinators = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator_diagnostics = []
    for codes, coordinator in coordinators.items():
        raw = coordinator.data or []
        coordinator_diagnostics.append(
            {
                "stop_codes": list(codes),
                "last_update_success": coordinator.last_update_success,
                "call_count_today": coordinator.call_count_today,
                "last_rate_limit": _redact_rate_limit(coordinator.last_rate_limit),
                "raw_calls_count": len(raw),
                # Échantillon plafonné : un pôle multimodal peut renvoyer des
                # dizaines de passages, inutile de tout dumper pour diagnostiquer.
                "raw_calls_sample": [_call_summary(call) for call in raw[:15]],
            }
        )

    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": async_redact_data(entry.options, TO_REDACT),
            "version": entry.version,
        },
        "coordinators": coordinator_diagnostics,
    }


def _redact_rate_limit(rate_limit) -> dict[str, Any] | None:
    """Aplatit le RateLimitInfo du producteur en dict simple (ou None avant 1er appel)."""
    if not rate_limit:
        return None
    return {
        "remaining_day": rate_limit.remaining_day,
        "limit_day": rate_limit.limit_day,
        "remaining_second": rate_limit.remaining_second,
        "limit_second": rate_limit.limit_second,
    }
