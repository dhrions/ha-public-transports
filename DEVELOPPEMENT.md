# Développement — déploiement sur une instance HA réelle

Ce carnet consigne les pièges rencontrés en développant ce custom_component contre une
vraie instance Home Assistant, pour ne pas les redécouvrir à chaque session.

## Cible actuelle : hardy-hop

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

Ne fonctionne **qu'en session SSH interactive** (cf. § ci-dessus) :

```bash
ssh hassio@192.168.1.14   # session interactive
ha core restart           # tapé dans la session, jamais passé en argument à ssh
```

Alternative sans SSH : UI HA → *Paramètres → Système → Redémarrer*.

## Récupérer les logs pour débugger

Depuis la session SSH interactive :

```bash
ha core logs | grep -A60 "Traceback"
```

Filtrer uniquement sur `public_transports` noie souvent la vraie erreur sous le bruit des
logs `DEBUG` du coordinator (rafraîchissement toutes les 60s) — préférer chercher
`Traceback` avec suffisamment de contexte (`-A60` à `-A80` selon la profondeur de la
pile HA).
