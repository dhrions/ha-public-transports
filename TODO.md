# TODO

Backlog technique de **public_transports for Home Assistant** — le détail *comment faire*.
La direction produit (*quoi atteindre*) vit dans `ROADMAP.md`.

## Ergonomie de configuration (cf. ROADMAP 🟠)

- [ ] **Sélection d'un sous-ensemble de lignes** (ex. suivre 2 lignes sur les 5 qui
  circulent à un arrêt, sans tout suivre ni se limiter à une seule) : le sélecteur ligne
  du flow (config + options) est aujourd'hui un simple dropdown à choix unique
  (`vol.Required` + `SelectSelector` sans `multiple=True`). Passer `line` en sélection
  multiple change `line_filter` d'une chaîne unique vers une liste, ce qui impacte
  `call_matches()` (coordinator.py, comparaison d'égalité → appartenance à liste) et le
  filtrage de `PublicTransportsSensor._calls`. Décision à prendre avant implémentation :
  un sous-ensemble choisi fusionne-t-il en un seul capteur (comme « Toutes les lignes »)
  ou se découpe-t-il en un capteur par ligne choisie (comme `SPLIT_LINES`, mais restreint
  au sous-ensemble plutôt qu'à la totalité) ? Signalé par l'utilisateur le 2026-08-22 en
  répondant à l'implémentation de « Toutes les lignes » sur les pôles multimodaux.
