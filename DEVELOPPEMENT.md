# Développement — déploiement sur une instance HA réelle

Ce carnet consigne les pièges rencontrés en développant ce custom_component contre une
vraie instance Home Assistant, pour ne pas les redécouvrir à chaque session.

## Cible actuelle : vivid-yam

`vivid-yam` (Raspberry Pi 5, HAOS) sert désormais d'instance de test, en remplacement de
`hardy-hop` — accessible uniquement via reverse proxy HTTPS
(`https://homeassistant.rain.dhrions.fr`) avec un jeton longue durée. **Aucune route
SSH/Samba/réseau direct** depuis les machines de dev (LAN distinct, `192.168.1.0/24`
mais sur un site différent — `ping`/`ssh` vers l'IP LAN échouent en « No route to
host »). Le jeton fonctionne sur `/api/` (Core) mais est **rejeté (401)** par
`/api/hassio/` (Supervisor) — donc aucune installation/gestion d'add-on à distance
possible avec ce jeton ; Samba, en particulier, ne peut pas être installé depuis une
session Claude Code distante. Toute la section « Déploiement : Samba share » ci-dessous
ne s'applique qu'à `hardy-hop`.

Déploiement sur vivid-yam : uniquement via **HACS** (le dépôt est publié sur GitHub avec
des releases taguées), pas de synchro fichier directe. Séquence fiable :

1. **Publier une release GitHub taguée** (`gh release create vX.Y.Z --target master
   --notes "..."`) — HACS suit les *releases*, pas les commits ; un push sans release
   n'est jamais vu par HACS.
2. **Forcer HACS à re-scanner** : `POST
   /api/config/config_entries/entry/{hacs_entry_id}/reload` (trouver `hacs_entry_id` via
   `GET /api/config/config_entries/entry`, filtrer `domain == "hacs"`). Un simple appel
   à `update.install` peut échouer silencieusement si HACS a mis en cache « déjà à
   jour ».
3. **Installer la version ciblée explicitement** : service `update.install` sur
   `update.public_transports_update` avec `{"version": "vX.Y.Z"}`, même si
   `latest_version` affiche encore l'ancienne — HACS va chercher le tag à la demande.
4. **Piège découvert (2026-08-22) : appeler `update.install` deux fois de suite sur LA
   MÊME version cible ne réécrit PAS les fichiers sur disque** — HACS traite ça comme un
   no-op silencieux, `installed_version` ne bouge pas. Pour forcer un vrai
   réécrasement, faire un **cycle downgrade → upgrade** : installer une version
   antérieure existante, attendre quelques secondes, puis réinstaller la version cible.
   Signal fiable qu'un vrai changement de fichiers a eu lieu : l'attribut
   `release_summary` de l'entité `update.*` passe à `<ha-alert
   alert-type='error'>Restart of Home Assistant required</ha-alert>`.
5. **Redémarrer Core pour de vrai.** Un redémarrage déclenché via l'API REST (service
   `homeassistant.restart`) **ou via l'UI** (*Paramètres → Système → Redémarrer*) peut
   répondre vite/sans erreur sans avoir réellement cyclé le process Python — piège
   rencontré à plusieurs reprises (2026-08-22, puis à nouveau 2026-09-01 via l'UI cette
   fois) : le nouveau code sur disque n'était pas repris en mémoire malgré un appel
   « réussi » (capteurs déjà en place restés visibles, mais toujours avec l'ancienne
   logique). Le redémarrage fiable reste `ha core restart` tapé par l'utilisateur en
   **session SSH interactive** sur l'hôte (CLI Supervisor) — cohérent avec la
   restriction SSH documentée plus bas pour hardy-hop (Claude Code n'a de toute façon
   aucun accès SSH à vivid-yam) — mais un redémarrage via l'UI a fini par fonctionner le
   2026-09-01 : pas de garantie absolue dans un sens ou l'autre, **toujours vérifier
   après coup** plutôt que de faire confiance à la méthode utilisée.
   - **Vérification fiable qu'un redémarrage a vraiment rechargé le code** (au-delà de
     `installed_version`, qui ne reflète que l'état HACS sur disque, pas le process en
     mémoire) : appeler un endpoint qui n'existe que si le nouveau code tourne
     réellement. Si le déploiement ajoute/modifie `diagnostics.py`, `GET
     /api/diagnostics/config_entry/{entry_id}` (trouver l'`entry_id` via
     `/api/config/config_entries/entry`, cf. ci-dessous) répond `200` seulement si ce
     module est chargé — un `404` prouve sans ambiguïté que l'ancien process tourne
     encore.

### Diagnostiquer à distance sans accès SSH/filesystem

- `GET /api/error_log` est **bloqué par le reverse proxy** (404), même si les autres
  routes `/api/` répondent normalement — ne pas le confondre avec « endpoint
  inexistant côté HA ». La vraie source de vérité pour un traceback reste le fichier de
  log téléchargé depuis l'UI HA (*Paramètres → Système → Journaux → Télécharger le
  journal complet*) — à demander explicitement à l'utilisateur, aucun contournement API
  trouvé.
- `POST /api/template` (Jinja2 évalué côté serveur) est très utile pour introspecter
  l'état réel sans accès filesystem : `states | selectattr(...)`,
  `integration_entities("domain")`, `config_entry_id('entity_id')`,
  `device_attr(device_id, 'config_entries')`. Contrairement au filtrage manuel d'un
  export JSON de `/api/states`, ça évite les faux négatifs de recherche (un mauvais
  motif de filtre côté client peut faire croire qu'une entité est absente alors
  qu'elle existe, cf. incident ci-dessous).
- `/api/config/config_entries/entry` (non documenté officiellement dans la doc REST
  HA, mais fonctionnel sur cette instance) liste les entrées avec `entry_id`/`domain`/
  `state`/`reason` (ce dernier très utile : une entrée en échec de setup y affiche le
  message d'erreur, ex. `Error communicating with API: 401 Client Error: Unauthorized
  ...`), utile pour retrouver l'`entry_id` de HACS ou d'une entrée `public_transports`
  sans passer par l'UI.
- `GET /api/diagnostics/config_entry/{entry_id}` renvoie le même export que
  *Télécharger les diagnostics* dans l'UI — depuis l'ajout de `diagnostics.py` (v0.7.3),
  c'est le **seul** canal qui expose `entry.data` **et** `entry.options` (caviardés,
  `api_token` retiré) à distance, sans SSH ni accès `.storage`. Utile aussi comme
  vérification de rechargement de code, cf. point 5 ci-dessus.
  - **Piège découvert le 2026-09-01 en s'en servant pour diagnostiquer un pôle
    multimodal dont les capteurs affichaient tous la même ligne** :
    `entry_sense_specs()` calcule `{**entry.data, **entry.options}["senses"]` — si les
    deux contiennent une clé `senses`, **`entry.options` écrase silencieusement
    `entry.data`**. Un bug de l'écran d'options (corrigé au commit `0efa215`, v0.7.2)
    avait réécrit `entry.options.senses` en écrasant le `line_filter` propre de chaque
    capteur d'un pôle vers une seule ligne, alors que `entry.data.senses` (jamais
    modifié depuis la création) restait intact. Résultat : regarder uniquement
    `entry.data` dans un diagnostic fait conclure à tort que le storage est sain — il
    faut **toujours comparer `data` ET `options`** quand `entry_sense_specs()` ou tout
    autre code fusionnant les deux est en cause. Un simple redéploiement du correctif +
    redémarrage ne répare pas une entrée déjà corrompue de cette façon (les `options`
    corrompues restent sur disque) : il faut **supprimer et recréer l'entrée** (une
    entrée neuve n'a pas d'`options`, donc retombe sur `entry.data.senses`, sain par
    construction).
  - Piège annexe rencontré pendant cette recréation : le formulaire de config_flow
    demande le token **API PRIM** (`apiKey` UUID du compte
    prim.iledefrance-mobilites.fr), pas le jeton longue durée Home Assistant utilisé
    partout ailleurs dans ce carnet — les deux se ressemblent assez pour être confondus.
    Se tromper ne bloque pas la saisie (pas d'erreur de validation) mais fait échouer
    l'entrée en `setup_retry` avec `401 Unauthorized` côté API PRIM, visible dans le
    `reason` de `/api/config/config_entries/entry` (cf. point précédent).
- Piège de reproduction locale : les tests unitaires qui appellent une méthode de flow
  HA **directement** (ex. `flow.async_step_init()` sur une instance construite à la
  main) contournent le vrai `FlowManager`
  (`hass.config_entries.options.async_init()`/`hass.config_entries.async_setup()`) et
  peuvent donc rater des bugs qui n'apparaissent qu'en production sur une version HA
  plus récente que la dépendance de test épinglée du dépôt. Deux bugs réels sont passés
  entre les mailles ainsi le 2026-08-22 : une `property` HA dépréciée dont le *setter*
  avait été carrément supprimé (`AttributeError` seulement en prod), et
  `entity_category` qui doit être l'enum `EntityCategory.DIAGNOSTIC`, pas la chaîne
  `"diagnostic"` (rejeté par `entity_registry.async_get_or_create` avec un `ValueError`
  silencieux côté plateforme, sans remonter d'erreur au niveau de l'entrée). Pour un
  test qui doit vraiment couvrir ce genre de régression, passer par le vrai
  `FlowManager`/`entity_registry`, pas par un appel direct à la méthode.

## Cible historique : hardy-hop

`hardy-hop` (HAOS, Home Assistant Yellow) sert d'instance de test. Adresse actuelle :
`192.168.1.14` (temporairement au site Maine — vérifier avant de copier des commandes,
cf. le dépôt `hardy-hop` pour l'adresse à jour).

## Pourquoi pas de déploiement en SSH direct

Une commande SSH **non-interactive** (`ssh host "commande"`, ce que fait aussi `rsync`)
tourne côté hardy-hop en tant qu'utilisateur `hassio` non privilégié (`uid=1000`, zéro
capability Linux). Une session SSH **interactive** (juste `ssh hassio@<ip>`, sans commande)
élève en `root` via un script de login différent. Ce n'est **pas** lié au « mode
protection » de l'add-on *Advanced SSH & Web Terminal* (testé désactivé, aucun effet) —
c'est une restriction structurelle de cet add-on. Conséquence : `rsync`/`ssh host
"commande"` n'ont jamais d'accès écriture à `/config`, quel que soit le réglage. Idem pour
`ha core restart` passé en argument à `ssh` : ne fonctionne qu'en session interactive.

## Déploiement : Samba share

Solution retenue pour contourner cette limitation. Add-on officiel **Samba share**
installé sur hardy-hop (identifiants dédiés, `allow_hosts: 192.168.1.0/24`).

Montage sur la machine de dev (`lushy-rosemary`) — pas persistant par défaut, à remonter
après chaque redémarrage de la machine (ou ajouter au `fstab` si l'usage devient
récurrent) :

```bash
sudo mkdir -p /mnt/hardy-hop-config
sudo mount -t cifs //192.168.1.14/config /mnt/hardy-hop-config \
  -o username=<user_samba>,password=<pass_samba>,uid=$(id -u),gid=$(id -g)
```

Commande de déploiement, à relancer à chaque modification de code :

```bash
rsync -av --delete --exclude='__pycache__/' --inplace \
  custom_components/public_transports/ \
  /mnt/hardy-hop-config/custom_components/public_transports/
```

Deux options non négociables :

- `--exclude='__pycache__/'` : inutile de synchroniser le bytecode compilé, HA le
  régénère seul.
- `--inplace` : **indispensable**. Sans lui, `rsync` échoue systématiquement sur
  `__init__.py` avec `mkstemp ... No such file or directory` — le fichier temporaire
  caché que `rsync` crée avant renommage échoue sur ce partage CIFS précis
  (probablement parce que ce fichier est activement importé par Home Assistant Core en
  cours d'exécution). `--inplace` écrit directement dans le fichier cible et contourne
  le problème.

## Redémarrer Home Assistant après déploiement

Trois façons, par ordre de préférence :

1. **API REST + jeton** (cf. section suivante) — scriptable, pas de SSH.
2. **Session SSH interactive** (cf. § ci-dessus — ne fonctionne pas en non-interactif) :
   ```bash
   ssh hassio@192.168.1.14   # session interactive
   ha core restart           # tapé dans la session, jamais passé en argument à ssh
   ```
3. **UI HA** → *Paramètres → Système → Redémarrer*.

## Inspecter l'état et redémarrer sans SSH : API REST + jeton longue durée

L'API REST de Home Assistant Core s'authentifie par jeton et n'a **pas** la restriction
SSH ci-dessus (elle ne passe pas par le canal Supervisor). Elle permet de lire
l'état/attributs d'une entité et de redémarrer Core, entièrement scriptable.

Mise en place (une fois) : profil HA → *Sécurité* → *Jetons d'accès longue durée* →
*Créer un jeton*. À stocker hors du dépôt (fichier scratchpad de session, jamais
committé) — c'est un accès équivalent à un admin HA.

```bash
TOKEN="<jeton>"

# Lire l'état/attributs d'une entité — utile pour vérifier un fix sans capture d'écran
curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.1.14:8123/api/states/sensor.gaite_prochain_passage

# Redémarrer Core (curl se termine en erreur — la connexion est coupée par le
# redémarrage lui-même — mais l'action a bien lieu ; revérifier avec un GET /api/
# qui redevient 401/200 une fois Core reparti)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://192.168.1.14:8123/api/services/homeassistant/restart
```

Limite : `GET /api/error_log` renvoie `404` sur cette instance (cohérent avec l'absence
d'un `home-assistant.log` courant sur le disque, cf. section suivante) — l'API ne
contourne pas ce problème précis, seule la session SSH interactive donne accès à une
vraie trace d'erreur.

## Récupérer les logs pour débugger

Depuis la session SSH interactive (l'API `/api/error_log` échoue en `404`, cf.
ci-dessus) :

```bash
ha core logs | grep -A60 "Traceback"
```

Filtrer uniquement sur `public_transports` noie souvent la vraie erreur sous le bruit des
logs `DEBUG` du coordinator (rafraîchissement toutes les 60s) — préférer chercher
`Traceback` avec suffisamment de contexte (`-A60` à `-A80` selon la profondeur de la
pile HA).
