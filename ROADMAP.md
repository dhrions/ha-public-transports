# Roadmap

Feuille de route de **public_transports for Home Assistant**
(jalons organisés par priorité — le détail d'implémentation vit dans `TODO.md`)

---

## 🔴 Priorité très haute

### Suivi temps réel de base
- [x] **Suivi d'un arrêt CTS (Strasbourg/Schiltigheim)** : découverte automatique des
  arrêts via l'API SIRI-lite native
- [x] **Suivi d'un arrêt IDF Mobilités / RATP (Paris, via PRIM)** : recherche d'arrêt par
  nom via le référentiel ouvert IDFM, faute de découverte SIRI native sur ce produit
- [x] **Filtrage par sens réel de circulation** : un capteur dédié par sens (DirectionRef
  SIRI), y compris sur une ligne fourchue à plusieurs terminus par sens

---

## 🟠 Priorité haute

### Ergonomie de configuration
- [x] **Configuration guidée et lisible** : libellés traduits (FR/EN), listes déroulantes
  recherchables pour la ville et l'arrêt
- [x] **Token API non redemandé** : réutilisation silencieuse du token déjà saisi pour la
  même compagnie de transport
- [x] **Suivre plusieurs lignes d'un même arrêt sans réajouter l'intégration** : un capteur
  par ligne (× sens) détectée à l'arrêt, en une seule configuration — limité aux arrêts à
  code physique unique (PRIM, ou un nom CTS non ambigu ; un capteur ne lit qu'un stop_code)

### Richesse des données exposées
- [x] **Accès direct aux horaires des prochains passages** sans parcourir une liste
  imbriquée d'objets

---

## 🟡 Priorité moyenne

### Regroupement d'arrêts colocalisés
- [ ] **Suivre en un seul capteur plusieurs arrêts d'un même pôle multimodal** (ex. bus et
  métro d'une même place), à partir de la zone de correspondance officielle IDFM
  (`zdcid`) plutôt que d'un rapprochement par nom — un nom similaire ne garantit pas la
  colocalisation (vérifié : deux arrêts « Châtelet » distincts peuvent être à l'autre
  bout de Paris l'un de l'autre)

### Couverture réseau
- [ ] **Extension aux réseaux déjà annoncés mais non câblés** : TCL (Lyon), RTM
  (Marseille), Lignes d'Azur (Nice), TBM (Bordeaux) — présents dans la liste des villes
  supportées mais sans configuration d'API SIRI-lite associée

---

## 🟢 Priorité basse

### Personnalisation du suivi
- [ ] **Intervalle de rafraîchissement configurable** depuis l'UI (actuellement fixe, 60s)

---

## 🔵 Long terme

### Distribution
- [ ] **Publication officielle sur HACS** (aujourd'hui installable uniquement en dépôt
  personnalisé)
