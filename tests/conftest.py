"""Fixtures for testing."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


# Entrée CTS minimale partagée par les fichiers de test qui n'ont besoin que d'un
# arrêt/token valides, sans les champs spécifiques (quiet_hours, senses...) que certains
# ajoutent par-dessus avec {**BASE_ENTRY_DATA, ...}.
BASE_ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
}
