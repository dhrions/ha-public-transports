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

## Documentation

- [x] En-tête README sans auteur ni ligne « Version X.Y.Z, DD/MM/YYYY » (README.md:1-4) — auteur + version ajoutés
- [x] Pas de ligne de version dans le README malgré `manifest.json` à 0.8.2 (README.md:1-4) — même correction
- [x] Titre H1 avec émoji 📚 non conforme à la charte (README.md:1) — corrigé (règle explicite : `conventions-documentation.adoc:333`, « titre sans émoji, l'image de dépôt porte déjà l'identité visuelle » ; sa présence sur d'autres dépôts du parc était une dérive répliquée, pas un précédent à suivre)
- [x] Section « Installation » utilise 🔧 au lieu de 🚀 (README.md:19) — corrigé
- [x] Absence de section « 💻 Utilisation » attendue pour l'archétype applicatif (README.md) — ajoutée (carte Lovelace, template, automation)
- [x] Section « 🚧 Hors périmètre actuel » duplique un item de ROADMAP.md sans lien vers celle-ci (README.md:138-140, ROADMAP.md:58) — contenu corrigé et lien vers ROADMAP.md ajouté
- [x] Aucune structure Antora : `docs/antora.yml`, `docs/antora-playbook.yml`, pages `.adoc`, `nav.adoc` tous absents (dépôt) — créée (arbitrage tranché avec l'utilisateur : créer plutôt qu'assumer README-only), build Antora validé localement
- [x] `.gitea/workflows/docs.yml` absent, donc pas de trigger cross-repo doc — ajouté (template standard, branche `master`) ; secret `REPOS_META_DISPATCH_TOKEN` déjà posé côté Gitea
- [x] `.repo-meta.json` minimal — `category`, `status`, `icon`, `description` absents (.repo-meta.json) — champs ajoutés (alignés sur `freebox-tools`)
- [x] Divergence structurelle avec `freebox-tools` (même catégorie IoT & Hardware, lui a une doc Antora complète) — résorbée, structure alignée
- [x] README, section « Hors périmètre » : « Intervalle de rafraîchissement configurable » est faux — implémenté depuis longtemps (README.md:140 vs README.md:83-93) ; ROADMAP.md:58 porte la même erreur (item non coché sous 🟢 Priorité basse alors que livré) — les deux corrigés
- [x] Offset de temps de marche (v0.8.0/0.8.1) totalement absent du README — le sens de l'état du capteur n'est plus documenté (config_flow.py:1071-1105, sensor.py:130-144) — section ajoutée
- [x] Sélection d'un sous-ensemble de lignes (multi-select) non répercutée dans le README, qui ne décrit que « une ligne ou toutes » (config_flow.py:653, README.md:48-50) — corrigé

## Sécurité & CI

- [x] Token API loggé en clair au niveau DEBUG lors de la découverte des arrêts — caviarder `headers`/`auth` avant `_LOGGER.debug` (config_flow.py:370-372) — corrigé
- [x] Aucun dispositif de scan de secrets (`secrets-scan.yml`/`.gitleaks.toml` absents) (.gitea/workflows/) — les deux ajoutés depuis les templates du parc
- [x] `hacs.yml` sous `.github/workflows/` à confirmer comme non-doublon d'une CI Gitea attendue (.github/workflows/hacs.yml:1) — confirmé non-doublon, annoté en commentaire

## Conception

- [x] `config_flow.py` (1127 lignes / 4 responsabilités indépendantes) à scinder en sous-package (config_flow.py:1-1127) — scindé en config_flow/{helpers,flow,options_flow}.py, 134/134 tests toujours au vert
- [x] Duplication exacte de `_coordinators` entre deux classes de sensor.py (sensor.py:212-214, 285-287) — factorisée dans `_QuotaRegistryEntity`

## Dépendances

- [x] Pas de bornage sur `pytest`, `pytest-cov`, `pytest-homeassistant-custom-component` (requirements.test.txt:1-3) — planchers ajoutés
- [x] Pas de workflow `audit.yml` (veille `pip-audit`) (.gitea/workflows/) — ajouté, adapté (pas de pyproject.toml ici : pip-audit sur un requirements assemblé depuis manifest.json + requirements.test.txt, écart assumé au template standard)
- [x] `syrupy` utilisé en plugin pytest sans être déclaré dans `requirements.test.txt` (setup.cfg:18) — déclaré

## Distribution (cf. ROADMAP 🔵)

> Deux voies distinctes, indépendantes l'une de l'autre : le dépôt HACS par défaut
> (magasin intégré, barre administrative) et Home Assistant Core (livré avec HA, barre
> technique — cf. blocage ci-dessous). Discuté le 2026-09-03 ; ne pas confondre les deux
> dans une seule case ROADMAP à cocher globalement.

- [ ] **Dépôt HACS par défaut** : ouvrir une PR sur `home-assistant/brands` avec
  l'icône/logo pour le domaine `public_transports` (obligatoire, indépendant de HACS —
  sert aussi si la voie Core est visée un jour), puis une PR d'ajout sur `hacs/default`
  et passer la validation automatique (action `hacs/action`). Prérequis déjà en place
  côté dépôt (`hacs.json`, releases taguées via semantic-release, README, manifest
  conforme).

- [ ] **Home Assistant Core — bloqué en l'état** : Core interdit l'I/O synchrone
  (`requests`), exige `aiohttp` via `async_get_clientsession`. Vérifié le 2026-09-03 :
  `siri-lite==0.12.0` (dépendance de ce dépôt) importe `requests` dans `discovery.py`,
  `web.py`, `http_client.py`, `lines.py`, `icar.py` — entièrement synchrone. Avant toute
  soumission Core, il faut réécrire `siri-lite` en async (ou l'envelopper), puis viser le
  Quality Scale (Bronze a minima : `DataUpdateCoordinator`, erreurs typées, pas d'appel
  bloquant, typing strict, `strings.json` complet). Ne pas soumettre à Core avant ce
  refactor — pas un point de process, un blocage technique dur.

## Tests

- [x] `resolve_line_label`/`resolve_line_names` : chemin d'erreur HTTP jamais testé (config_flow.py:178-215, tests/test_config_flow.py:21-22) — 3 tests ajoutés (404, ClientError, succès)
- [x] `probe_available_passages` : comportement d'échec (`try/except`) jamais testé directement, toujours mocké (config_flow.py:111-126, tests/test_config_flow.py:467-743) — 2 tests ajoutés, sans mocker la fonction elle-même
- [x] `async_migrate_entry` (migrations v1→v2, v2→v3) totalement non testé — risque de casse silencieuse des configs utilisateurs en prod (__init__.py:24-47) — 3 tests ajoutés (v1→v3, v2→v3, no-op déjà à jour)
- [x] `diagnostics.py` aucun test, y compris la redaction du token (diagnostics.py) — tests/test_diagnostics.py créé, 4 tests, module à 100 % de couverture
