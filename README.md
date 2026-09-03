# 📚 Public Transports for Home Assistant

Dhrions
Version 0.8.2, 03/09/2026

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

## 🚀 Installation

1. Copier `custom_components/public_transports/` dans le dossier `custom_components/` de
   votre installation Home Assistant (ou installer via HACS en ajoutant ce dépôt comme
   dépôt personnalisé).
2. Redémarrer Home Assistant.
3. Ajouter l'intégration **Public Transports** depuis *Paramètres → Appareils et
   services → Ajouter une intégration*.

## ⚙️ Configuration

L'assistant de configuration guide en plusieurs étapes :

1. **Ville** — champ de recherche avec liste déroulante, parmi les villes ayant au moins
   une compagnie câblée (`Strasbourg`, `Schiltigheim`, `Paris`).
2. **Compagnie de transport** — CTS pour Strasbourg/Schiltigheim ; IDF Mobilités / RATP
   pour Paris.
3. **Token API** — nécessaire dans tous les cas : un token CTS (Basic Auth) pour la CTS,
   ou une clé API PRIM (header `apiKey`, à obtenir sur
   [prim.iledefrance-mobilites.fr](https://prim.iledefrance-mobilites.fr/)) pour IDF
   Mobilités / RATP.
4. **Arrêt** — recherche par nom (ex. « Gaîté »), puis sélection dans la liste des
   résultats : plusieurs arrêts physiques peuvent porter le même nom (bus/métro/tram
   distingués, ex. « Gaîté — Paris 14e (métro) » vs « Gaîté — Clamart (bus) »).
   *(IDF Mobilités / RATP uniquement)* Si l'arrêt choisi appartient à un pôle multimodal
   (zone de correspondance IDFM `zdcid` partagée avec d'autres arrêts, ex. quais bus +
   métro d'une même place), une étape supplémentaire propose de **regrouper tout le pôle
   dans un seul capteur** plutôt que de ne suivre que ce quai précis. Purement opt-in ; sans
   pôle détecté, cette étape n'apparaît pas.
5. **Ligne(s)** *(facultatif)* — sélection multiple : cocher un sous-ensemble des lignes
   qui circulent à cet arrêt (chaque ligne cochée produit son propre capteur), ou ne rien
   cocher pour suivre toutes les lignes. Les lignes proposées sont celles qui circulent au
   moment de la configuration (sondage temps réel de l'API). Ce choix ne se règle qu'à la
   création de l'entrée — il ne se modifie pas via les Options.
6. **Sens** *(facultatif)* — pour ne suivre qu'un sens de circulation (les 2 sens réels de
   la ligne, ex. « Châtillon Montrouge » vs « Asnières… / Saint-Denis… »), ou « Les deux
   sens (2 capteurs) » pour créer d'emblée un capteur par sens plutôt qu'un seul capteur
   non filtré. Le sens est déterminé par le `DirectionRef` SIRI, pas par le terminus : une
   ligne fourchue (ex. métro 13) a plusieurs terminus pour un même sens, tous regroupés.

Le filtre ligne/sens est **modifiable après coup** sans supprimer l'arrêt : *Paramètres →
Appareils et services → Public Transports →* menu ⋮ de l'entrée *→ Options*.

Une entrée de configuration = un arrêt suivi (éventuellement restreint à une ligne / un sens).

### 🚶 Temps pour rejoindre l'arrêt

Optionnel, réglable dans les Options de l'entrée (*Temps pour rejoindre l'arrêt (min)*).
Change le **sens de l'état** du capteur : au lieu de l'heure d'arrivée brute du prochain
passage, l'état devient **« dans combien de temps partir »** pour l'attraper — (minutes du
prochain passage encore rattrapable) − ce temps de marche. Un passage à 20 min avec un
temps de marche de 8 min affiche un état `12`. Un passage trop proche pour être rattrapé
n'apparaît plus comme état (mais reste listé dans `next_times`/`next_passages`, qui gardent
toujours les **heures d'arrivée réelles**, non décalées). Aucun passage rattrapable → état
`unknown`. Une valeur à `0` (par défaut) restaure le comportement d'origine : état = heure
d'arrivée brute.

Sur une entrée à plusieurs capteurs (plusieurs lignes/sens), une étape avancée permet de
**surcharger ce réglage par capteur** plutôt que de subir le même temps de marche pour
tous — utile quand les quais ne sont pas à la même distance du point de départ.

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

## 💻 Utilisation

Le capteur s'utilise comme n'importe quel capteur numérique Home Assistant — carte
Lovelace, automation, template. Exemple de carte affichant l'état (temps restant, en
minutes) et la ligne/destination du prochain passage rattrapable :

```yaml
type: entities
entities:
  - entity: sensor.gaite_prochain_passage
    name: Prochain métro
    secondary_info: last-changed
```

Exemple de template exploitant `next_times` pour afficher les 3 prochains passages sans
filtrer sur le sens :

```yaml
{{ state_attr('sensor.gaite_prochain_passage', 'next_times')[:3] | join(', ') }} min
```

Une automation peut se déclencher sur l'état numérique, par exemple pour notifier quand il
reste moins de 5 minutes pour partir :

```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.gaite_prochain_passage
    below: 5
```

## ⏱️ Fréquence de rafraîchissement et quota d'API

Par défaut, le capteur est rafraîchi **toutes les 60 secondes**, avec un **créneau de
silence de 23:00 à 07:00** (heure locale de Home Assistant) : pendant ce créneau aucun
appel n'est émis et la dernière valeur connue est conservée. Fréquence (de 1 s à 10 min,
valeur libre — le sélecteur propose des présets usuels mais accepte n'importe quel entier
de secondes dans cette plage) et créneau de silence sont modifiables : *Paramètres →
Appareils et services → Public Transports →* menu ⋮ de l'entrée *→ Options*. Le formulaire
affiche l'estimation du volume quotidien pour le réglage choisi et **refuse un réglage qui
dépasserait le quota** effectif
du jeton.

⚠️ **Le quota d'API est compté par token, pas par intégration** : un même token peut
alimenter plusieurs instances HA et d'autres clients. Et un appel est émis **par code
d'arrêt et par cycle** — une entrée « les deux sens » (CTS) ou un pôle multimodal à N quais
multiplie d'autant :

| Configuration | Appels/cycle | 60 s, 24 h/24 | 60 s, silence 23:00–07:00 |
|---|---|---|---|
| 1 arrêt simple | 1 | 1 440/jour | 960/jour |
| « Les deux sens » (CTS) | 2 | 2 880/jour | 1 920/jour |
| Pôle à N quais | N | 1 440 × N | 960 × N |

Pour PRIM (IDF Mobilités / RATP), le quota contractuel est de **1 000 requêtes/jour**
pour un jeton généré entre le 13/03 et septembre 2024 (« nouvel utilisateur » — un jeton
plus ancien monte à 1 000 000/jour) ; source :
[fiche officielle de l'API](https://prim.iledefrance-mobilites.fr/fr/apis/idfm-ivtr-requete_unitaire),
section « Accès à l'API », consultée le 2026-08-20. Le créneau de silence nocturne par
défaut ramène un arrêt simple à 60 s sous ce palier. Pas de chiffre équivalent trouvé pour
CTS à ce jour. Pour sonder en continu, mettre le même début et la même fin de créneau (ex.
`00:00`/`00:00`).

Un **capteur « quota API »** dédié (catégorie diagnostic) expose, quand le producteur le
fournit dans ses en-têtes (cas de PRIM ; absent sur CTS), le quota quotidien restant. Le
quota est plafonné par le producteur **par transporteur et par endpoint** (confirmé sur
PRIM), pas par entrée : plusieurs arrêts PRIM configurés avec le même jeton partagent donc
**un seul** capteur (`IDF Mobilités / RATP - quota API`) plutôt que d'afficher chacun sa
propre lecture du même compteur.

La part consommée par cette intégration seule (tous arrêts confondus, pour ce transporteur)
est exposée par un **second capteur dédié**, `<transporteur> - appels effectués
aujourd'hui` — plutôt qu'en attribut du premier : un attribut n'a pas d'historique HA
propre, ce qui empêchait tout graphique de la consommation dans le temps. Ce capteur ne
dépend pas des en-têtes du producteur (disponible même sur CTS) et repart de zéro à chaque
changement de jour **ou à chaque redémarrage de Home Assistant** — le compteur n'est pas
persisté, un redémarrage en cours de journée sous-estime donc le total réel pour le reste
de la journée.

## 📦 Dépendances

Cette intégration s'appuie sur [`siri-lite`](https://pypi.org/project/siri-lite/)
(`>=0.10.0`, publié sur PyPI) pour interroger l'API SIRI-lite *StopMonitoring* et récupérer
les prochains passages. La découverte des arrêts (`stoppoints-discovery`, utilisée lors de
la configuration) reste implémentée directement dans cette intégration.

## 🚧 Hors périmètre actuel

- Extension aux réseaux annoncés mais non encore câblés côté API SIRI-lite : TCL (Lyon),
  RTM (Marseille), Lignes d'Azur (Nice), TBM (Bordeaux).
- Publication officielle sur HACS (installable uniquement en dépôt personnalisé pour
  l'instant).
