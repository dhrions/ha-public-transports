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

## Validation en usage réel (v0.8.x, sur `vivid-yam`)

> Les tests unitaires passent, mais le comportement visible des deux features ci-dessus
> n'a pas encore été confirmé à la main dans l'interface HA. Une fois vérifié, cocher.

- [ ] **Tester l'offset de temps de marche.** Paramètres → Appareils et services →
  Public Transports → une entrée → *Configurer*. Mettre p. ex. `8` dans « Temps pour
  rejoindre l'arrêt (min) », Valider. Sémantique retenue = **« avant de partir »** :
  l'**état** = (minutes du prochain passage rattrapable) − offset (ex. passages 3/20/28 →
  état `12`, car le 3 est manqué et 20 − 8 = 12), ou `unknown` si aucun n'est rattrapable.
  Les **attributs** `next_times` / `next_passages` gardent les **heures d'arrivée réelles**
  de tous les passages (3, 20, 28) — l'offset ne s'applique qu'à l'état. Les scalaires de
  tête (`line`/`destination`) décrivent le passage rattrapable (celui de l'état), pas
  l'imminent. Offset `0` → état = heure d'arrivée brute (comportement d'origine).

- [ ] **Tester l'override d'offset par ligne/sens (étape avancée).** Même écran, activer
  « Régler l'offset par ligne/sens », Valider → une 2ᵉ page s'ouvre avec un champ par
  capteur (libellé = ligne + sens). Mettre une valeur sur un seul, laisser les autres
  vides, Valider. Attendu : seul ce capteur applique son offset propre ; les autres gardent
  le défaut de l'entrée.

- [ ] **Tester le sous-ensemble de lignes.** Ne se règle qu'à la **création** d'une entrée
  (le choix de ligne ne se refait pas via les options). *Ajouter une entrée* sur un arrêt
  multi-lignes (ex. un pôle type Gaîté), et à l'étape « Ligne(s) » décocher certaines
  lignes. Attendu : seuls les capteurs des lignes cochées sont créés (un par ligne × sens),
  aucun pour les lignes décochées. Tout coché (ou rien) = toutes les lignes.
