# 📚 public_transports for Home Assistant

> Intégration custom Home Assistant (HACS) affichant les prochains passages de transport
> en commun à un arrêt configuré, via [siri-lite](https://pypi.org/project/siri-lite/).

## ⚡ TL;DR

- 🚌 Configure un arrêt (ville, compagnie, arrêt) via l'UI Home Assistant et expose un
  capteur : temps restant avant le prochain passage, ligne, destination.
- 🎯 Réseau supporté aujourd'hui : **CTS (Strasbourg)**. D'autres compagnies SIRI-lite
  peuvent être ajoutées dans `const.py`.
- 🚀 Installation : copier `custom_components/public_transports/` dans le dossier
  `custom_components` de Home Assistant (ou via HACS), redémarrer, puis ajouter
  l'intégration **Public Transports** depuis les paramètres.

## Installation

1. Copier `custom_components/public_transports/` dans le dossier `custom_components/` de
   votre installation Home Assistant (ou installer via HACS en ajoutant ce dépôt comme
   dépôt personnalisé).
2. Redémarrer Home Assistant.
3. Ajouter l'intégration **Public Transports** depuis *Paramètres → Appareils et
   services → Ajouter une intégration*.

## Configuration

L'assistant de configuration guide en 4 étapes :

1. **Ville** (ex. `Strasbourg`).
2. **Compagnie de transport** — pour Strasbourg/Schiltigheim, la CTS (Compagnie des
   Transports Strasbourgeois) est actuellement la seule compagnie câblée.
3. **Token API** — un token CTS est nécessaire (authentification Basic Auth auprès de
   l'API SIRI-lite de la CTS).
4. **Arrêt** — sélection dans la liste des arrêts découverts pour la compagnie choisie.

Une entrée de configuration = un arrêt suivi.

## Capteur

Chaque arrêt configuré crée un capteur `sensor.<nom_arrêt>_prochain_passage` :

| Champ | Contenu |
|---|---|
| État | Temps restant avant le prochain passage, en minutes |
| `line` | Référence de la ligne |
| `published_line_name` | Nom publié de la ligne |
| `destination` | Destination du véhicule |
| `next_passages` | Liste des temps restants (minutes) pour tous les passages retournés |

Le capteur est rafraîchi toutes les 60 secondes (intervalle fixe pour l'instant, non
configurable via l'UI).

## Dépendances

Cette intégration s'appuie sur [`siri-lite`](https://pypi.org/project/siri-lite/)
(`>=0.6.0`, publié sur PyPI) pour interroger l'API SIRI-lite *StopMonitoring* et récupérer
les prochains passages. La découverte des arrêts (`stoppoints-discovery`, utilisée lors de
la configuration) reste implémentée directement dans cette intégration.

## Hors périmètre actuel

- PRIM / Île-de-France Mobilités (siri-lite le supporte, pas encore câblé côté intégration).
- Intervalle de rafraîchissement configurable.
