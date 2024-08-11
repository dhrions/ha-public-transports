DOMAIN = "public_transports"

# Données des villes et entreprises de transport
CITIES_DATA = {
    "Paris": ["RATP", "Transdev"],
    "Lyon": ["TCL"],
    "Marseille": ["RTM"],
    "Nice": ["Lignes d'Azur"],
    "Bordeaux": ["TBM"],
    "Strasbourg": ["CTS"],
    "Schiltigheim": ["CTS"]
}

# Configuration des compagnies de transport
TRANSIT_COMPANIES = {
    "CTS": {
        "protocol": "SIRI-lite",
        "api_url": "https://api.cts-strasbourg.eu/v1/siri/2.0",
        "requires_token": True
    },
    # Ajoutez ici d'autres entreprises avec leurs protocoles
    # Par exemple :
    # "RATP": {
    #     "protocol": "SIRI-lite",
    #     "api_url": "https://api.ratp.fr/siri/2.0",
    #     "requires_token": True
    # },
    # "TCL": {
    #     "protocol": "GTRS-RT",
    #     "api_url": None,
    #     "requires_token": False
    # }
}
