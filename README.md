# 📚 Public Transports for Home Assistant

> Intégration custom Home Assistant (HACS) affichant les prochains passages de transport
> en commun à un arrêt configuré, via [siri-lite](https://pypi.org/project/siri-lite/).

## ⚡ TL;DR

- 🚌 Configure un arrêt (ville, compagnie, arrêt) via l'UI Home Assistant et expose un
  capteur : temps restant avant le prochain passage, ligne, destination.
- 🎯 Réseaux supportés aujourd'hui : **CTS (Strasbourg/Schiltigheim)** et **IDF Mobilités /
  RATP (Paris, via PRIM)**. L'API PRIM souscrite n'expose pas `stoppoints-discovery` ; la
  découverte des arrêts pour Paris passe donc par le référentiel public IDFM
  `zones-d-arrets` à la place. D'autres compagnies SIRI-lite peuvent être ajoutées dans
  `const.py`.
- 🚀 Installation : copier `custom_components/public_transports/` dans le dossier
  `custom_components` de Home Assistant (ou via HACS), redémarrer, puis ajouter
  l'intégration **Public Transports** depuis les paramètres.

## 🔧 Installation

1. Copier `custom_components/public_transports/` dans le dossier `custom_components/` de
   votre installation Home Assistant (ou installer via HACS en ajoutant ce dépôt comme
   dépôt personnalisé).
2. Redémarrer Home Assistant.
3. Ajouter l'intégration **Public Transports** depuis *Paramètres → Appareils et
   services → Ajouter une intégration*.

## ⚙️ Configuration

L'assistant de configuration guide en plusieurs étapes :

1. **Ville** — parmi les villes ayant au moins une compagnie câblée (`Strasbourg`,
   `Schiltigheim`, `Paris`).
2. **Compagnie de transport** — CTS pour Strasbourg/Schiltigheim ; IDF Mobilités / RATP
   pour Paris.
3. **Token API** — nécessaire dans tous les cas : un token CTS (Basic Auth) pour la CTS,
   ou une clé API PRIM (header `apiKey`, à obtenir sur
   [prim.iledefrance-mobilites.fr](https://prim.iledefrance-mobilites.fr/)) pour IDF
   Mobilités / RATP.
4. **Arrêt** — sélection dans la liste des arrêts découverts pour la compagnie choisie.
   *(IDF Mobilités / RATP uniquement)* Si l'arrêt choisi appartient à un pôle multimodal
   (zone de correspondance IDFM `zdcid` partagée avec d'autres arrêts, ex. quais bus +
   métro d'une même place), une étape supplémentaire propose de **regrouper tout le pôle
   dans un seul capteur** plutôt que de ne suivre que ce quai précis. Purement opt-in ; sans
   pôle détecté, cette étape n'apparaît pas.
5. **Ligne** *(facultatif)* — pour ne suivre qu'une ligne précise à cet arrêt, ou
   « Toutes les lignes ». Les lignes proposées sont celles qui circulent au moment de la
   configuration (sondage temps réel de l'API).
6. **Sens** *(facultatif)* — pour ne suivre qu'un sens de circulation (les 2 sens réels de
   la ligne, ex. « Châtillon Montrouge » vs « Asnières… / Saint-Denis… »), ou « Les deux
   sens (2 capteurs) » pour créer d'emblée un capteur par sens plutôt qu'un seul capteur
   non filtré. Le sens est déterminé par le `DirectionRef` SIRI, pas par le terminus : une
   ligne fourchue (ex. métro 13) a plusieurs terminus pour un même sens, tous regroupés.

Le filtre ligne/sens est **modifiable après coup** sans supprimer l'arrêt : *Paramètres →
Appareils et services → Public Transports →* menu ⋮ de l'entrée *→ Options*.

Une entrée de configuration = un arrêt suivi (éventuellement restreint à une ligne / un sens).

## 📡 Capteur

Chaque arrêt configuré crée un capteur `sensor.<nom_arrêt>_prochain_passage` :

| Champ | Contenu |
|---|---|
| État | Temps restant avant le prochain passage, en minutes |
| `stop_code` | Code d'arrêt physique effectivement interrogé (ex. quai précis d'un arrêt CTS à plusieurs codes) — pour un pôle regroupé, le code primaire (premier arrêt choisi) |
| `stop_codes` | Liste de tous les codes d'arrêt physiques interrogés par ce capteur — un seul élément hors regroupement de pôle, plusieurs pour un pôle multimodal |
| `line` | Référence de la ligne |
| `published_line_name` | Nom publié de la ligne |
| `destination` | Terminus du véhicule (prochain passage) |
| `next_times` | Liste plate des minutes des prochains passages (ex. `next_times[1]` = passage suivant), pratique sur un dashboard/en template sans fouiller `next_passages` |
| `next_passages` | Liste de tous les passages retournés : `{time, line, destination}` chacun — le terminus par passage y reste visible même quand le capteur est filtré sur un sens |

Quand un filtre ligne et/ou sens est appliqué, le **nom du capteur** le reflète (ex.
`Gaîté 13 → Châtillon Montrouge - prochain passage`), et l'état/la liste ne comptent que
les passages du sens choisi. Sans filtre, le nom reste `<nom_arrêt> - prochain passage` et
tous les passages sont pris en compte. Le terminus précis de chaque rame reste dans
`next_passages`.

## ⏱️ Fréquence de rafraîchissement et quota d'API

Par défaut, le capteur est rafraîchi **toutes les 2 minutes, entre 07:00 et 20:00**
(heure locale de Home Assistant). Hors de cette plage, aucun appel n'est émis et la
dernière valeur connue est conservée. Les deux réglages sont modifiables : *Paramètres →
Appareils et services → Public Transports →* menu ⋮ de l'entrée *→ Options*.

⚠️ **Ces valeurs par défaut existent pour protéger un quota d'API partagé.** Le quota est
compté **par token**, pas par intégration : un même token peut alimenter plusieurs
instances HA et d'autres clients. Et un appel est émis **par code d'arrêt et par cycle** —
une entrée « les deux sens » (CTS) ou un pôle multimodal à N quais multiplie d'autant :

| Configuration | Appels/cycle | 60 s, 24 h/24 | 2 min, 07:00–20:00 |
|---|---|---|---|
| 1 arrêt simple | 1 | 1 440/jour | 390/jour |
| « Les deux sens » (CTS) | 2 | 2 880/jour | 780/jour |
| Pôle à N quais | N | 1 440 × N | 390 × N |

Avec un quota courant de 1 000 appels/jour, l'ancien défaut (60 s en continu) le dépassait
donc **avec un seul arrêt**. Pour désactiver complètement la restriction horaire, mettre
la même valeur en début et en fin de plage (ex. `00:00`/`00:00`).

## 📦 Dépendances

Cette intégration s'appuie sur [`siri-lite`](https://pypi.org/project/siri-lite/)
(`>=0.10.0`, publié sur PyPI) pour interroger l'API SIRI-lite *StopMonitoring* et récupérer
les prochains passages. La découverte des arrêts (`stoppoints-discovery`, utilisée lors de
la configuration) reste implémentée directement dans cette intégration.

## 🚧 Hors périmètre actuel

- Intervalle de rafraîchissement configurable.
