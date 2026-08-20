from datetime import timedelta

DOMAIN = "public_transports"

# Le quota de l'API est compté par token, pas par intégration : un même token peut être
# partagé par plusieurs instances HA et par d'autres clients (extension GNOME, scripts).
# Un coordinateur émet un appel PAR code d'arrêt et par cycle, donc une entrée « pôle »
# à N quais ou « les deux sens » multiplie d'autant. À 60 s en continu, un seul arrêt
# consommait déjà 1440 appels/jour, au-delà des 1000/jour du palier PRIM « nouvel
# utilisateur » (jeton généré entre le 13/03 et sept. 2024 — cf. fiche officielle
# https://prim.iledefrance-mobilites.fr/fr/apis/idfm-ivtr-requete_unitaire, section
# « Accès à l'API », consultée le 2026-08-20 ; le débit associé, 5 req/s, n'est jamais
# approché par ce coordinateur). D'où un défaut à 2 min, restreint à une plage active.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=2)

CONF_SCAN_INTERVAL = "scan_interval"
CONF_ACTIVE_START = "active_start"
CONF_ACTIVE_END = "active_end"

# Minutes, bornes de l'intervalle réglable dans les options.
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 60

# Plage horaire active par défaut (heure locale HA). Hors de cette plage, le coordinateur
# n'appelle pas l'API et conserve la dernière donnée connue.
DEFAULT_ACTIVE_START = "07:00"
DEFAULT_ACTIVE_END = "20:00"

# Référentiel public IDFM des zones d'arrêt (StopArea), sans authentification.
# zdaid <n> correspond directement au MonitoringRef SIRI STIF:StopArea:SP:<n>:
# (vérifié le 2026-08-19 : zdaid=45102 -> "Châtelet - Les Halles", cf. notes
# personnelles siri-lite sur STIF:StopArea:SP:45102:).
IDFM_ZONES_API_URL = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets/zones-d-arrets/records"

# Référentiel public IDFM arrêts+lignes, sans authentification. Le champ "id" (ex.
# "IDFM:C01383") correspond au code SIRI LineRef (ex. "STIF:Line::C01383:") ; ses
# "route_long_name"/"shortname" donnent le nom lisible ("13") absent du PublishedLineName
# de certaines réponses stop-monitoring PRIM (vérifié le 2026-08-19).
IDFM_LINES_API_URL = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets/arrets-lignes/records"

# Données des villes et entreprises de transport
CITIES_DATA = {
    "Paris": ["IDF Mobilités / RATP"],
    "Lyon": ["TCL"],
    "Marseille": ["RTM"],
    "Nice": ["Lignes d'Azur"],
    "Bordeaux": ["TBM"],
    "Strasbourg": ["Compagnie des Transports Strasbourgeois"],
    "Schiltigheim": ["Compagnie des Transports Strasbourgeois"]
}

# Configuration des compagnies de transport
TRANSIT_COMPANIES = {
    "Compagnie des Transports Strasbourgeois": {
        "protocol": "SIRI-lite",
        "api_url": "https://api.cts-strasbourg.eu/v1/siri/2.0",
        "endpoint" : "/stoppoints-discovery",
        "stop_monitoring_endpoint": "/stop-monitoring?MonitoringRef=",
        "auth_type" : "Basic Auth",
        "requires_token": True,
        "discovery_backend": "siri"
    },
    "IDF Mobilités / RATP": {
        "protocol": "SIRI-lite",
        "api_url": "https://prim.iledefrance-mobilites.fr/marketplace",
        "stop_monitoring_endpoint": "/stop-monitoring?MonitoringRef=",
        "auth_type": "apiKey",
        "requires_token": True,
        # IDFM n'expose pas stoppoints-discovery sur ce produit PRIM (403 permanent,
        # cf. authentication.adoc du dépôt siri-lite) : la recherche d'arrêt passe par
        # le référentiel ouvert IDFM_ZONES_API_URL plutôt que par l'API SIRI elle-même.
        "discovery_backend": "idfm_zones"
    },
    # Ajoutez ici d'autres entreprises avec leurs protocoles
    # Par exemple :
    # "TCL": {
    #     "protocol": "GTRS-RT",
    #     "api_url": None,
    #     "requires_token": False
    # }
}
