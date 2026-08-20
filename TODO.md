# TODO

Backlog technique de **public_transports for Home Assistant** — le détail *comment faire*.
La direction produit (*quoi atteindre*) vit dans `ROADMAP.md`.

## Regroupement d'arrêts colocalisés (cf. ROADMAP 🟡)

Suivre en un seul capteur plusieurs arrêts d'un même pôle multimodal (ex. bus + métro
d'une même place), à partir du `zdcid` officiel IDFM (zone de correspondance) — jamais par
rapprochement de nom (vérifié : des arrêts homonymes peuvent être à l'autre bout de Paris
l'un de l'autre, ex. deux « Châtelet » avec des `zdcid` différents, quand d'autres
partagent bien le même `zdcid` malgré des noms distincts).

Prérequis architectural : aujourd'hui un capteur (`sensor.py`) lit un seul `stop_code` via
un seul `coordinator` (`__init__.py` en crée un par `stop_code` distinct de l'entrée,
`coordinator.py`). Un capteur « pôle multimodal » doit agréger plusieurs `stop_code` —
comparable en ampleur au refactor qui a permis « les deux sens = 2 capteurs »
(`entry_sense_specs`/`call_matches`, `coordinator.py`).

Pistes de décomposition (à affiner avant de démarrer, via `/roadmap --milestone` ou en
reprenant ce fichier) :

- [ ] **Découverte du pôle** : à la sélection d'un arrêt PRIM (`async_step_get_stop_select`,
  `config_flow.py`), si le `zdaid` choisi partage son `zdcid` avec d'autres arrêts du
  référentiel `zones-d-arrets`, proposer une option opt-in « inclure tout le pôle »
  (nouvelle requête `IDFM_ZONES_API_URL` filtrée sur `zdcid`).
- [ ] **Modèle de spec multi-code** : étendre `entry_sense_specs` (`coordinator.py`) pour
  qu'une spec porte une liste de `stop_code` au lieu d'un seul, ou introduire un type de
  spec dédié « pôle ».
- [ ] **Capteur multi-coordinator** : `PublicTransportsSensor` (`sensor.py`) doit pouvoir
  lire plusieurs `coordinator.data` et fusionner leurs passages (tri par temps restant)
  au lieu d'un seul flux brut.
- [ ] **`__init__.py`** : créer un coordinator par `stop_code` du pôle (déjà le cas côté
  mécanique — à vérifier que la construction du dict `coordinators` par entrée reste
  correcte quand plusieurs specs partagent des `stop_code` qui se recoupent partiellement).
- [ ] **Non-régression CTS** : cette capacité est PRIM-only (CTS n'a pas de notion de
  `zdcid`) — s'assurer que le chemin CTS n'est pas affecté.

Portée : PRIM uniquement (CTS n'a pas d'équivalent au `zdcid`).
