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

# URL des API SIRI-lite pour chaque société de transport et besoin en token
SIRI_API_URLS = {
    "CTS": {
        "url": "https://api.cts-strasbourg.eu/v1/siri/2.0",
        "requires_token": True
    },
    # Ajoutez ici d'autres entreprises si nécessaire
}
