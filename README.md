# RSEQ Hockey Scraper — module complémentaire Home Assistant / Home Assistant add-on

![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5.svg)
![Architectures](https://img.shields.io/badge/arch-amd64%20%7C%20aarch64-lightgrey.svg)

[Français](#français) | [English](#english)

---

## Français

### Description

**RSEQ Hockey Scraper** lit les données d'une équipe de hockey scolaire du RSEQ sur le site
**scolaire.rseqhockey.com** (plateforme Spordle) et les publie dans Home Assistant sous forme de
**capteurs MQTT** (découverte automatique *MQTT Discovery*) : horaire, dernier match, alignement,
statistiques des joueurs, classement et, si désiré, les statistiques d'un joueur suivi.

Le site est protégé par Cloudflare. Le module passe donc par **FlareSolverr** (service externe
obligatoire) pour obtenir les témoins (*cookies*) Cloudflare, puis par **Playwright** (Chromium sans
interface) pour afficher l'horaire de toute la saison. Les logos des équipes sont téléchargés dans
`/config/www/rseqhockey_logos/` et servis par Home Assistant sous `/local/rseqhockey_logos/`.

### Fonctionnalités

- Prochain match et liste des matchs à venir (date, heure, aréna, équipes, logos).
- Dernier match joué avec le score, et historique de tous les matchs joués (conservé d'un cycle à l'autre).
- Alignement : gardiens, joueurs (nom, numéro, statistiques) et personnel.
- Statistiques de la saison régulière de tous les joueurs de la ligue.
- Classement de la division de l'équipe, rang et classement de la ligue complète.
- Capteur facultatif pour un joueur suivi (points en saison régulière).
- **Rythme de mise à jour adaptatif** les jours de match (toutes les 10 min pendant un match).

### Installation

1. **Prérequis**
   - un courtier MQTT (par exemple le module **Mosquitto broker**) et l'intégration **MQTT** configurée ;
   - un service **FlareSolverr** joignable depuis Home Assistant (conteneur Docker
     `ghcr.io/flaresolverr/flaresolverr`, port 8191 par défaut, ou un module complémentaire FlareSolverr
     d'un dépôt communautaire) ;
   - un système **amd64** ou **aarch64** (seules architectures déclarées).
2. Ajouter le dépôt : **Paramètres → Modules complémentaires → Boutique → ⋮ (en haut à droite) → Dépôts**,
   coller `https://github.com/Frazou1/reseqhockey-ha-addon`, puis **Ajouter**. Ou cliquer sur ce bouton :

   [![Ajouter le dépôt dans Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FFrazou1%2Freseqhockey-ha-addon)
3. Rafraîchir la boutique, ouvrir **RSEQ Hockey Scraper** et cliquer sur **Installer** (l'image Playwright
   est volumineuse : l'installation peut prendre plusieurs minutes).
4. Remplir l'onglet **Configuration** (voir plus bas) — en particulier `team_id`, `team_name` et
   `flaresolverr_url` — puis **Démarrer**.
5. Ouvrir l'onglet **Journal** : on doit voir les étapes FlareSolverr, Playwright, l'analyse, puis
   `[MQTT SUCCESS]` pour chaque sujet et `[ATTENTE] N min`.

### Configuration

| Option | Type | Défaut dans config.yaml | Rôle |
|---|---|---|---|
| `team_id` | texte | *(valeur de l'auteur, à remplacer)* | Identifiant de l'équipe sur scolaire.rseqhockey.com : le nombre dans l'URL `https://scolaire.rseqhockey.com/fr/teams/<team_id>`. Sert aussi dans les identifiants des capteurs. |
| `team_name` | texte | *(valeur de l'auteur, à remplacer)* | Nom de l'équipe tel qu'il est écrit dans le **classement** (comparaison sans tenir compte des majuscules, mais les accents comptent). Sert à trouver la division et le rang. |
| `tracked_player` | texte, facultatif | *(valeur de l'auteur, à remplacer ou vider)* | Partie du nom d'un joueur à suivre (ex. son nom de famille). Vide = pas de capteur de joueur. |
| `season_start` | texte, facultatif | `2026-09-19` | **Non utilisé** par le code actuel. |
| `season_end` | texte, facultatif | `2027-04-01` | **Non utilisé** par le code actuel. |
| `timezone` | texte, facultatif | `America/Toronto` | **Non utilisé** : l'heure des matchs est toujours convertie en `America/Toronto`. |
| `update_interval_hours` | entier | `6` | Intervalle normal entre deux lectures, en heures. |
| `mqtt_host` | texte | `core-mosquitto` | Hôte du courtier MQTT. |
| `mqtt_port` | entier | `1883` | Port du courtier MQTT. |
| `mqtt_user` | texte, facultatif | `""` | Utilisateur MQTT (utilisé seulement si le mot de passe est aussi rempli). |
| `mqtt_password` | texte, facultatif | `""` | Mot de passe MQTT. |
| `flaresolverr_url` | texte | *(adresse de l'auteur, à remplacer)* | URL de l'API FlareSolverr, terminée par `/v1`. |
| `flaresolverr_timeout` | entier | `90` | Délai maximal accordé à FlareSolverr, en secondes. |

Exemple :

```yaml
team_id: "<identifiant de l'équipe>"
team_name: "NOM DE L'ÉQUIPE DANS LE CLASSEMENT"
tracked_player: "nomdefamille"
season_start: "2026-09-19"
season_end: "2027-04-01"
timezone: America/Toronto
update_interval_hours: 6
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: mon_utilisateur_mqtt
mqtt_password: mon_mot_de_passe_mqtt
flaresolverr_url: http://<adresse-flaresolverr>:8191/v1
flaresolverr_timeout: 90
```

> **Important — ligue fixée dans le code.** Le classement et les statistiques des joueurs sont lus dans
> une catégorie de ligue et un calendrier (saison régulière) **fixés dans `main.py`**
> (`LEAGUE_UUID`, `SCHEDULE_SAISON_REGULIERE`). L'horaire, l'alignement et le dernier match suivent bien
> `team_id`, mais pour une équipe d'une autre ligue, le classement et les statistiques seront vides ou
> faux tant que ces deux valeurs ne sont pas modifiées.

### Entités créées

Le module crée un appareil MQTT **« RSEQ Hockey Équipe &lt;team_id&gt; »**. Home Assistant ne tient plus compte
de l'`object_id` : l'`entity_id` vient du nom de l'appareil et du nom du capteur, p. ex.
`sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_equipe_<team_id>_horaire` ou
`sensor.rseq_hockey_equipe_<team_id>_rseq_dernier_match`. À vérifier dans **Paramètres → Appareils et services →
MQTT → Appareils**.

| Capteur (nom affiché) | Identifiant unique | État | Attributs |
|---|---|---|---|
| RSEQ Prochain match | `rseqhockey_<team_id>_schedule` | `<date> <heure> - <aréna>` ou `Aucun match prévu` | `prochain_match`, `matchs_a_venir`, `nb_total` |
| RSEQ Dernier match | `rseqhockey_<team_id>_last_match` | `<date> : <équipe 1> <équipe 2> <score>` ou `Aucun match joué` | `match`, `tous_les_matchs_joues` |
| RSEQ Alignement | `rseqhockey_<team_id>_roster` | `N joueurs \| M instructeurs` | `gardiens`, `joueurs` (`nom`, `numero`, `stats`), `personnel` (`nom`, `role`) |
| RSEQ Stats Saison | `rseqhockey_<team_id>_stats_saison_reguliere` | `N joueurs` | `joueurs` (une ligne par joueur, clés = colonnes du site, ex. `Nom`, `PTS`) |
| RSEQ Classement | `rseqhockey_<team_id>_standings` | `<rang>e / <nb d'équipes>` ou `Hors classement` | `mon_equipe`, `classement_complet` (division), `rang_ligue`, `nb_equipes_ligue`, `classement_ligue` |
| RSEQ Joueur &lt;Nom&gt; *(si `tracked_player`)* | `rseqhockey_player_<tracked_player>` | `SR: N pts` | `stats_saison_reguliere` |

Le capteur de classement n'est publié que si l'équipe a été trouvée dans le classement.

Chaque match (`prochain_match`, éléments de `matchs_a_venir`, `match`, `tous_les_matchs_joues`) contient :
`numero`, `date` (ex. `SAM. 4 OCT.`), `date_iso`, `heure` (ex. `17h45`), `iso` (date-heure du site),
`arene`, `equipes`, `equipes_details` (`nom`, `logo_remote`, `logo_local`), `score` (ex. `3-2`),
`statut` (`À VENIR`, `FINAL`, `EN COURS`, `PROLONGATION`, `FUSILLADE`) et `joue`.

Les lignes de classement contiennent les colonnes du site (`#`, `Équipe`, `MJ`, `V`, `D`, `N`, `PTS`…)
plus `logo_remote` et `logo_local`.

Sujets MQTT : `homeassistant/sensor/<identifiant>/config` (préfixe de découverte fixe) et
`rseqhockey/<team_id>/<capteur>/state` / `attributes` (ou `rseqhockey/player/<nom>/…`), messages conservés.

### Exemples d'utilisation

Carte Markdown avec le prochain match et le classement :

```yaml
type: markdown
title: Hockey
content: >
  {% set p = state_attr('sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_equipe_<team_id>_horaire', 'prochain_match') %}
  {% if p %}**Prochain match :** {{ p.date }} {{ p.heure }} — {{ p.equipes | join(' vs ') }} ({{ p.arene }}){% endif %}

  {% for e in state_attr('sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_classement_<ecole>', 'classement_complet') or [] %}
  {{ e['#'] }}. <img src="{{ e.logo_local }}" width="20"> {{ e['Équipe'] }} — {{ e.PTS }} pts
  {% endfor %}
```

Automatisation : notification quand un nouveau résultat est publié :

```yaml
alias: Hockey - Nouveau résultat
triggers:
  - trigger: state
    entity_id: sensor.rseq_hockey_equipe_<team_id>_rseq_dernier_match
conditions:
  - condition: template
    value_template: "{{ trigger.from_state is not none and trigger.to_state.state != trigger.from_state.state }}"
actions:
  - action: notify.notify
    data:
      message: "Résultat : {{ trigger.to_state.state }}"
```

### Rythme de mise à jour

Après chaque cycle, le module calcule le délai avant le suivant (`rythme.py`) à partir des matchs lus :

- **avant un match** : réveil à l'heure du début (si elle arrive avant l'intervalle normal) ;
- **pendant un match** : toutes les **10 minutes**, jusqu'à ce qu'un score soit affiché (4 h au plus) ;
- **après le match** : toutes les **heures** pendant **3 heures** ;
- **sinon** : l'intervalle normal (`update_interval_hours`).

Le délai minimal est de 60 secondes. Le journal l'indique : `[ATTENTE] N min (raison)`.

Fichiers persistants dans `/data` : `options.json` (configuration), `matchs_joues.json` (historique des
matchs joués), `rythme_fins.json` (fin détectée de chaque match, gardée 2 jours).
Autres fichiers : logos dans `/config/www/rseqhockey_logos/` ; pages de débogage dans `/share/`
(`schedule_debug.html`, `roster_debug.html`, `stats_saison_reguliere_debug.html`), réécrites à chaque cycle.

### Dépannage

Toujours commencer par l'onglet **Journal** du module.

| Message dans le journal | Cause probable | Solution |
|---|---|---|
| `[FLARESOLVERR ERROR] …` puis `[ABORT] Impossible d'obtenir les cookies Cloudflare.` | FlareSolverr injoignable, mauvaise URL ou délai trop court | Vérifier `flaresolverr_url` (avec `/v1`) et que FlareSolverr roule ; augmenter `flaresolverr_timeout`. Aucun capteur n'est mis à jour pendant ce cycle. |
| `[PLAYWRIGHT ERROR] Cloudflare non franchi.` | Défi Cloudflare non résolu | Réessayer au cycle suivant ; redémarrer FlareSolverr. L'horaire est alors vide, mais l'historique des matchs joués est conservé. |
| `✗ Échec clic 1/2/3` | Le site a changé ses boutons de filtre | L'horaire peut être incomplet ; consulter `/share/schedule_debug.html`. |
| `[PARSING STANDINGS] Équipe introuvable dans le classement.` | `team_name` différent du nom dans le classement, ou équipe d'une autre ligue | Copier le nom exact du classement (accents compris) ; voir la note « ligue fixée dans le code ». |
| `[MQTT ERROR] …` | Courtier injoignable ou identifiants refusés | Vérifier `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`. |
| `[LOGO ERROR] …` | Logo non téléchargeable | Sans gravité : `logo_local` reste vide pour cette équipe. |
| Capteur de joueur à `SR: 0 pts` | `tracked_player` ne correspond à aucun nom de la colonne `Nom` | Utiliser une partie du nom telle qu'elle apparaît dans les statistiques. |
| État affiché entre guillemets | Les états sont publiés encodés en JSON | Comportement actuel du module ; utiliser les attributs dans les cartes. |

### Limites connues

- Ligue (classement, statistiques) fixée dans le code : voir la note dans **Configuration**.
- `season_start`, `season_end` et `timezone` ne sont pas utilisés.
- Préfixe de découverte MQTT fixé à `homeassistant`.
- Un match sans score dont la date est passée (ex. annulé) reste dans `matchs_a_venir`.
- Dépend de la structure du site Spordle et de Cloudflare : une modification du site peut casser la lecture.

### Revenir à une version précédente

Les versions publiées sont taguées `vX.Y.Z` sur GitHub (onglet **Tags** du dépôt). Le Supervisor installe
toujours la dernière version de la branche `main`.

1. **Le plus simple** : restaurer la sauvegarde du module prise avant la mise à jour
   (**Paramètres → Système → Sauvegardes**, si « Sauvegarder avant la mise à jour » était coché).
2. **Sinon** : noter la configuration, désinstaller le module, retirer le dépôt, puis l'ajouter de nouveau
   avec la référence du tag : `https://github.com/Frazou1/reseqhockey-ha-addon#v3.3.2`. Réinstaller et
   recopier la configuration. Pour revenir à la dernière version, rajouter le dépôt sans `#…`.

---

## English

### Description

**RSEQ Hockey Scraper** reads a RSEQ school hockey team's data from **scolaire.rseqhockey.com** (Spordle
platform) and publishes it to Home Assistant as **MQTT sensors** (automatic *MQTT Discovery*): schedule,
last game, roster, player statistics, standings and, optionally, the statistics of one tracked player.

The site is protected by Cloudflare. The add-on therefore uses **FlareSolverr** (a required external
service) to get the Cloudflare cookies, then **Playwright** (headless Chromium) to display the full
season schedule. Team logos are downloaded to `/config/www/rseqhockey_logos/` and served by Home
Assistant under `/local/rseqhockey_logos/`.

### Features

- Next game and list of upcoming games (date, time, arena, teams, logos).
- Last game played with its score, and history of every played game (kept between cycles).
- Roster: goalies, players (name, number, statistics) and staff.
- Regular-season statistics for every player in the league.
- The team's division standings, league rank and full league standings.
- Optional sensor for a tracked player (regular-season points).
- **Adaptive update rhythm** on game days (every 10 min during a game).

### Installation

1. **Prerequisites**
   - an MQTT broker (for example the **Mosquitto broker** add-on) and the **MQTT** integration set up;
   - a **FlareSolverr** service reachable from Home Assistant (Docker container
     `ghcr.io/flaresolverr/flaresolverr`, port 8191 by default, or a FlareSolverr add-on from a community
     repository);
   - an **amd64** or **aarch64** system (the only declared architectures).
2. Add the repository: **Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories**, paste
   `https://github.com/Frazou1/reseqhockey-ha-addon`, then **Add**. Or click this button:

   [![Add the repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FFrazou1%2Freseqhockey-ha-addon)
3. Refresh the store, open **RSEQ Hockey Scraper** and click **Install** (the Playwright image is large:
   installation can take several minutes).
4. Fill in the **Configuration** tab (see below) — especially `team_id`, `team_name` and
   `flaresolverr_url` — then **Start**.
5. Open the **Log** tab: you should see the FlareSolverr and Playwright steps, the parsing, then
   `[MQTT SUCCESS]` for each topic and `[ATTENTE] N min` (waiting N min).

### Configuration

| Option | Type | Default in config.yaml | Purpose |
|---|---|---|---|
| `team_id` | string | *(author's value, replace it)* | Team ID on scolaire.rseqhockey.com: the number in the URL `https://scolaire.rseqhockey.com/fr/teams/<team_id>`. Also used in the sensors' IDs. |
| `team_name` | string | *(author's value, replace it)* | Team name as written in the **standings** (case-insensitive match, but accents matter). Used to find the division and rank. |
| `tracked_player` | string, optional | *(author's value, replace or clear it)* | Part of the name of a player to follow (e.g. the last name). Empty = no player sensor. |
| `season_start` | string, optional | `2026-09-19` | **Not used** by the current code. |
| `season_end` | string, optional | `2027-04-01` | **Not used** by the current code. |
| `timezone` | string, optional | `America/Toronto` | **Not used**: game times are always converted to `America/Toronto`. |
| `update_interval_hours` | integer | `6` | Normal interval between two reads, in hours. |
| `mqtt_host` | string | `core-mosquitto` | MQTT broker host. |
| `mqtt_port` | integer | `1883` | MQTT broker port. |
| `mqtt_user` | string, optional | `""` | MQTT user (used only when the password is also set). |
| `mqtt_password` | string, optional | `""` | MQTT password. |
| `flaresolverr_url` | string | *(author's address, replace it)* | FlareSolverr API URL, ending with `/v1`. |
| `flaresolverr_timeout` | integer | `90` | Maximum time given to FlareSolverr, in seconds. |

Example:

```yaml
team_id: "<team id>"
team_name: "TEAM NAME AS SHOWN IN THE STANDINGS"
tracked_player: "lastname"
season_start: "2026-09-19"
season_end: "2027-04-01"
timezone: America/Toronto
update_interval_hours: 6
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: my_mqtt_user
mqtt_password: my_mqtt_password
flaresolverr_url: http://<flaresolverr-address>:8191/v1
flaresolverr_timeout: 90
```

> **Important — league hard-coded.** Standings and player statistics are read from a league category and
> a (regular-season) schedule that are **hard-coded in `main.py`** (`LEAGUE_UUID`,
> `SCHEDULE_SAISON_REGULIERE`). Schedule, roster and last game do follow `team_id`, but for a team in
> another league, standings and statistics will be empty or wrong until those two values are changed.

### Created entities

The add-on creates an MQTT device **"RSEQ Hockey Équipe &lt;team_id&gt;"**. Home Assistant no longer uses
`object_id`: the `entity_id` comes from the device name and the sensor name, e.g.
`sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_equipe_<team_id>_horaire` or `sensor.rseq_hockey_equipe_<team_id>_rseq_dernier_match`.
Check it in **Settings → Devices & services → MQTT → Devices**. Sensor names and states are in French.

| Sensor (display name) | Unique ID | State | Attributes |
|---|---|---|---|
| RSEQ Prochain match | `rseqhockey_<team_id>_schedule` | `<date> <time> - <arena>` or `Aucun match prévu` | `prochain_match`, `matchs_a_venir`, `nb_total` |
| RSEQ Dernier match | `rseqhockey_<team_id>_last_match` | `<date> : <team 1> <team 2> <score>` or `Aucun match joué` | `match`, `tous_les_matchs_joues` |
| RSEQ Alignement | `rseqhockey_<team_id>_roster` | `N joueurs \| M instructeurs` | `gardiens`, `joueurs` (`nom`, `numero`, `stats`), `personnel` (`nom`, `role`) |
| RSEQ Stats Saison | `rseqhockey_<team_id>_stats_saison_reguliere` | `N joueurs` | `joueurs` (one row per player, keys = site columns, e.g. `Nom`, `PTS`) |
| RSEQ Classement | `rseqhockey_<team_id>_standings` | `<rank>e / <number of teams>` or `Hors classement` | `mon_equipe`, `classement_complet` (division), `rang_ligue`, `nb_equipes_ligue`, `classement_ligue` |
| RSEQ Joueur &lt;Name&gt; *(if `tracked_player`)* | `rseqhockey_player_<tracked_player>` | `SR: N pts` | `stats_saison_reguliere` |

The standings sensor is only published when the team is found in the standings.

Each game (`prochain_match`, items of `matchs_a_venir`, `match`, `tous_les_matchs_joues`) contains:
`numero`, `date` (e.g. `SAM. 4 OCT.`), `date_iso`, `heure` (e.g. `17h45`), `iso` (site date-time),
`arene`, `equipes`, `equipes_details` (`nom`, `logo_remote`, `logo_local`), `score` (e.g. `3-2`),
`statut` (`À VENIR`, `FINAL`, `EN COURS`, `PROLONGATION`, `FUSILLADE`) and `joue` (played).

Standings rows contain the site's columns (`#`, `Équipe`, `MJ`, `V`, `D`, `N`, `PTS`…) plus
`logo_remote` and `logo_local`.

MQTT topics: `homeassistant/sensor/<id>/config` (fixed discovery prefix) and
`rseqhockey/<team_id>/<sensor>/state` / `attributes` (or `rseqhockey/player/<name>/…`), retained messages.

### Usage examples

Markdown card with the next game and the standings:

```yaml
type: markdown
title: Hockey
content: >
  {% set p = state_attr('sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_equipe_<team_id>_horaire', 'prochain_match') %}
  {% if p %}**Next game:** {{ p.date }} {{ p.heure }} — {{ p.equipes | join(' vs ') }} ({{ p.arene }}){% endif %}

  {% for e in state_attr('sensor.rseq_hockey_equipe_<team_id>_rseq_hockey_classement_<ecole>', 'classement_complet') or [] %}
  {{ e['#'] }}. <img src="{{ e.logo_local }}" width="20"> {{ e['Équipe'] }} — {{ e.PTS }} pts
  {% endfor %}
```

Automation: notification when a new result is published:

```yaml
alias: Hockey - New result
triggers:
  - trigger: state
    entity_id: sensor.rseq_hockey_equipe_<team_id>_rseq_dernier_match
conditions:
  - condition: template
    value_template: "{{ trigger.from_state is not none and trigger.to_state.state != trigger.from_state.state }}"
actions:
  - action: notify.notify
    data:
      message: "Result: {{ trigger.to_state.state }}"
```

### Update rhythm

After each cycle, the add-on computes the delay before the next one (`rythme.py`) from the games it read:

- **before a game**: wake up at the start time (if it comes before the normal interval);
- **during a game**: every **10 minutes**, until a score is shown (4 h at most);
- **after the game**: **hourly** for **3 hours**;
- **otherwise**: the normal interval (`update_interval_hours`).

The minimum delay is 60 seconds. The log shows it: `[ATTENTE] N min (reason)`.

Persistent files in `/data`: `options.json` (configuration), `matchs_joues.json` (played-games history),
`rythme_fins.json` (detected end of each game, kept 2 days).
Other files: logos in `/config/www/rseqhockey_logos/`; debug pages in `/share/` (`schedule_debug.html`,
`roster_debug.html`, `stats_saison_reguliere_debug.html`), rewritten on every cycle.

### Troubleshooting

Always start with the add-on's **Log** tab.

| Log message | Likely cause | Fix |
|---|---|---|
| `[FLARESOLVERR ERROR] …` then `[ABORT] Impossible d'obtenir les cookies Cloudflare.` | FlareSolverr unreachable, wrong URL or timeout too short | Check `flaresolverr_url` (with `/v1`) and that FlareSolverr is running; raise `flaresolverr_timeout`. No sensor is updated during that cycle. |
| `[PLAYWRIGHT ERROR] Cloudflare non franchi.` | Cloudflare challenge not solved | Wait for the next cycle; restart FlareSolverr. The schedule is then empty, but the played-games history is kept. |
| `✗ Échec clic 1/2/3` | The site changed its filter buttons | The schedule may be incomplete; look at `/share/schedule_debug.html`. |
| `[PARSING STANDINGS] Équipe introuvable dans le classement.` | `team_name` differs from the name in the standings, or the team is in another league | Copy the exact name from the standings (accents included); see the "league hard-coded" note. |
| `[MQTT ERROR] …` | Broker unreachable or credentials refused | Check `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`. |
| `[LOGO ERROR] …` | Logo could not be downloaded | Harmless: `logo_local` stays empty for that team. |
| Player sensor at `SR: 0 pts` | `tracked_player` matches no name in the `Nom` column | Use part of the name as it appears in the statistics. |
| State shown between quotes | States are published JSON-encoded | Current add-on behaviour; use the attributes in cards. |

### Known limitations

- League (standings, statistics) hard-coded: see the note in **Configuration**.
- `season_start`, `season_end` and `timezone` are not used.
- MQTT discovery prefix fixed to `homeassistant`.
- A game with no score whose date has passed (e.g. cancelled) stays in `matchs_a_venir`.
- Depends on the Spordle site layout and on Cloudflare: a site change can break the scraping.

### Rolling back to a previous version

Released versions are tagged `vX.Y.Z` on GitHub (the repository's **Tags** tab). The Supervisor always
installs the latest version from the `main` branch.

1. **Easiest**: restore the add-on backup taken before the update (**Settings → System → Backups**, if
   "Back up before updating" was checked).
2. **Otherwise**: write down the configuration, uninstall the add-on, remove the repository, then add it
   again with the tag reference: `https://github.com/Frazou1/reseqhockey-ha-addon#v3.3.2`. Reinstall and
   paste the configuration back. To return to the latest version, add the repository again without `#…`.
