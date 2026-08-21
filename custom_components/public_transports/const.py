from datetime import timedelta

DOMAIN = "public_transports"

# Le quota de l'API est compté par token, pas par intégration : un même token peut être
# partagé par plusieurs instances HA et par d'autres clients (extension GNOME, scripts).
# Un coordinateur émet un appel PAR code d'arrêt et par cycle, donc une entrée « pôle »
# à N quais ou « les deux sens » multiplie d'autant. À 60 s en continu, un seul arrêt
# consomme 1440 appels/jour, au-delà des 1000/jour du palier PRIM « nouvel utilisateur »
# (jeton généré entre le 13/03 et sept. 2024 — cf. fiche officielle
# https://prim.iledefrance-mobilites.fr/fr/apis/idfm-ivtr-requete_unitaire, section
# « Accès à l'API », consultée le 2026-08-20 ; le débit associé, 5 req/s, n'est jamais
# approché par ce coordinateur ; un jeton plus ancien monte à 1 000 000/jour).
#
# Deux garde-fous côté intégration : (1) le formulaire d'options affiche l'estimation du
# volume quotidien induit et bloque l'enregistrement si elle dépasse le quota réel lu dans
# les en-têtes de réponse (cf. coordinator.estimate_daily_calls / siri-lite RateLimitInfo) ;
# (2) un créneau de silence nocturne par défaut (cf. DEFAULT_QUIET_*) ramène un arrêt simple
# à 60 s sous le palier 1000/jour. Le défaut de fréquence reste 60 s : réactif, et le vrai
# quota du jeton (lu en direct) est la référence, pas un palier codé en dur.
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)

# Présets proposés pour la fréquence de rafraîchissement (en secondes), éditable par
# l'utilisateur via les Options de l'entrée. Plancher à 1s ; un plafond de 10min reste
# utile pour un usage occasionnel.
SCAN_INTERVAL_OPTIONS = {
    1: "1 seconde",
    5: "5 secondes",
    10: "10 secondes",
    30: "30 secondes",
    60: "1 minute",
    120: "2 minutes",
    300: "5 minutes",
    600: "10 minutes",
}

# Bornes (en secondes) appliquées à la LECTURE de scan_interval, pas seulement à la
# saisie : une valeur éditée à la main dans .storage (hors dropdown) ne peut donc pas
# faire descendre l'intervalle sous 1s et marteler l'API.
MIN_SCAN_INTERVAL_SECONDS = 1
MAX_SCAN_INTERVAL_SECONDS = 600

# Créneau de silence nocturne proposé par défaut (heure locale HA). Pendant ce créneau,
# aucun appel n'est émis et la dernière donnée connue est conservée. 23:00→07:00 = 8h de
# silence : à 60 s, un arrêt simple tombe à 16h × 60/h = 960 appels/jour, sous le palier
# PRIM 1000/jour. Mettre début == fin (ex. 00:00/00:00) désactive le créneau (sonde 24/7).
DEFAULT_QUIET_HOURS_START = "23:00:00"
DEFAULT_QUIET_HOURS_END = "07:00:00"

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
