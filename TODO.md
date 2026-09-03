# TODO

Backlog technique de **public_transports for Home Assistant** — le détail *comment faire*.
La direction produit (*quoi atteindre*) vit dans `ROADMAP.md`.

## Ergonomie de configuration (cf. ROADMAP 🟠)

- [x] **Sélection d'un sous-ensemble de lignes** (ex. suivre 2 lignes sur les 5 qui
  circulent à un arrêt, sans tout suivre ni se limiter à une seule) : implémenté en
  passant le step `select_line` en `SelectSelector(multiple=True)` — `line_filter` reste
  une chaîne unique **par spec**, chaque ligne cochée produisant son propre capteur (comme
  l'ancien `SPLIT_LINES`, mais restreint aux lignes choisies au lieu de la totalité) ;
  `call_matches()` n'a donc pas eu besoin de changer. Rien coché = toutes les lignes.

- [x] **Seuil de temps de marche** (masquer les passages trop proches pour être
  attrapés) : offset en minutes, cascade à 2 niveaux — défaut par entrée
  (`entry_walking_time`, couvre toute la zone de correspondance sur un pôle) surchargeable
  par capteur/ligne×sens (`spec_walking_time`, étape avancée des Options). `0` = désactivé.
