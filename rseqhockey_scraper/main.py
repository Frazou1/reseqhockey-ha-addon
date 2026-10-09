import json
import time
import re
import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt

with open("/data/options.json", "r") as f:
    config = json.load(f)

TEAM_ID = config.get("team_id", "179927")
MY_TEAM_NAME = config.get("team_name", "ÉCOLE SEC. DE LA SEIGNEURIE").upper()
TRACKED_PLAYER = config.get("tracked_player", "").strip().lower()

MQTT_HOST = config.get("mqtt_host", "core-mosquitto")
MQTT_PORT = config.get("mqtt_port", 1883)
MQTT_USER = config.get("mqtt_user", "")
MQTT_PASS = config.get("mqtt_password", "")
INTERVAL = config.get("update_interval_hours", 6) * 3600

FLARESOLVERR_URL = config.get("flaresolverr_url", "http://192.168.2.65:8191/v1")
FLARESOLVERR_TIMEOUT = config.get("flaresolverr_timeout", 90)

LEAGUE_UUID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SCHEDULE_SAISON_REGULIERE = "198862"

LOGOS_DIR = "/share/rseqhockey_logos"
WWW_LOGOS_DIR = "/config/www/rseqhockey_logos"
os.makedirs(LOGOS_DIR, exist_ok=True)

if os.path.exists("/config/www") and not os.path.exists(WWW_LOGOS_DIR):
    try:
        os.symlink(LOGOS_DIR, WWW_LOGOS_DIR)
    except Exception as e:
        print(f"[LOGOS Note] {e}")

DEVICE_INFO = {
    "identifiers": [f"rseqhockey_team_{TEAM_ID}"],
    "name": f"RSEQ Hockey Équipe {TEAM_ID}",
    "model": "RSEQ Scraper",
    "manufacturer": "Spordle / RSEQ"
}


# ---------- FLARESOLVERR ----------

async def fetch_with_flaresolverr(session, url, key_name):
    print(f"\n[FLARESOLVERR] Requête '{key_name}': {url}")
    payload = {"cmd": "request.get", "url": url, "maxTimeout": FLARESOLVERR_TIMEOUT * 1000}
    try:
        async with session.post(FLARESOLVERR_URL, json=payload) as resp:
            data = await resp.json()
    except Exception as e:
        print(f"[FLARESOLVERR ERROR '{key_name}'] {e}")
        return None

    if data.get("status") != "ok":
        print(f"[FLARESOLVERR ERROR '{key_name}'] Status: {data.get('status')} | {data.get('message')}")
        return None

    html = data.get("solution", {}).get("response", "")
    if not html:
        print(f"[FLARESOLVERR ERROR '{key_name}'] Réponse vide.")
        return None

    lower = html.lower()
    if "vérification de sécurité en cours" in lower or "just a moment" in lower:
        print(f"[FLARESOLVERR WARN '{key_name}'] Challenge encore présent.")
        return None

    print(f"[FLARESOLVERR OK '{key_name}'] {len(html)} octets reçus.")
    return html


# ---------- MQTT ----------

def publish_to_mqtt(topic, payload):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()

    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    try:
        client.connect(MQTT_HOST, int(MQTT_PORT), keepalive=10)
        result = client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)
        result.wait_for_publish(timeout=5)
        if result.rc != 0:
            print(f"[MQTT WARN] rc={result.rc} sur {topic}")
        client.disconnect()
        print(f"[MQTT SUCCESS] Publié sur: {topic}")
    except Exception as e:
        print(f"[MQTT ERROR] Échec sur {topic}: {e}")


# ---------- HELPERS ----------

def split_name_and_number(raw: str) -> tuple:
    """
    Découpe une chaîne du type 'AAANNABELLEMICHAUD35' en ('ANNABELLE MICHAUD', '35').
    Le HTML de Spordle concatène prénom + nom + numéro sans séparateur,
    avec parfois des lettres dupliquées parasites au début (ex: 'AAA', 'EEE').
    """
    raw = raw.strip()
    if not raw:
        return "", ""

    # 1. Extraire le numéro à la fin
    num_match = re.search(r'(\d+)$', raw)
    numero = num_match.group(1) if num_match else ""
    name_part = raw[:num_match.start()] if num_match else raw

    # 2. Nettoyer les lettres dupliquées parasites au début (AAA, EEE, etc.)
    #    Ex: 'AAANNABELLE' -> 'ANNABELLE', 'EEELLIOT' -> 'ELLIOT'
    name_part = re.sub(r'^(.)\1+', r'\1', name_part, count=1)

    # 3. Séparer prénom/nom : on cherche une majuscule après au moins 2 minuscules
    #    Ex: 'ANNABELLEMICHAUD' -> 'ANNABELLE MICHAUD'
    #    Astuce : on insère un espace avant toute majuscule qui suit une minuscule
    #    ou avant une séquence majuscule qui termine un prénom
    parts = re.split(r'(?<=[a-zéèêëàâäîïôöùûüç])(?=[A-ZÉÈÊËÀÂÄÎÏÔÖÙÛÜÇ])', name_part)
    if len(parts) >= 2:
        # Regrouper : tout sauf le dernier = prénom, dernier = nom
        prenom = parts[0]
        nom = " ".join(parts[1:])
        full_name = f"{prenom} {nom}"
    else:
        # Fallback : tout en majuscules sans séparation détectable, on garde tel quel
        full_name = name_part

    # 4. Normaliser les espaces
    full_name = re.sub(r'\s+', ' ', full_name).strip()
    return full_name, numero


def clean_role(role: str) -> str:
    return role.strip()


# ---------- PARSING ROSTER ----------

def parse_roster_structured(html: str):
    try:
        with open("/data/roster_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    gardiens = []
    joueurs = []
    personnel = []

    for t_idx, table in enumerate(soup.find_all("table")):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not headers:
            continue

        header_set = set(h.lower() for h in headers)

        # --- Table des GARDIENS ---
        if "gardiens" in header_set:
            print(f"[ROSTER] Table #{t_idx} = GARDIENS")
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                full_name, numero = split_name_and_number(tds[0])
                if not full_name:
                    continue
                stats = dict(zip(headers, tds))
                gardiens.append({
                    "nom": full_name,
                    "numero": numero,
                    "stats": stats
                })

        # --- Table des JOUEURS ---
        elif "joueurs" in header_set:
            print(f"[ROSTER] Table #{t_idx} = JOUEURS")
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                full_name, numero = split_name_and_number(tds[0])
                if not full_name:
                    continue
                stats = dict(zip(headers, tds))
                joueurs.append({
                    "nom": full_name,
                    "numero": numero,
                    "stats": stats
                })

        # --- Table du PERSONNEL ---
        elif any("personnel" in h.lower() for h in headers):
            print(f"[ROSTER] Table #{t_idx} = PERSONNEL")
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                raw_name = tds[0]
                role = tds[1]
                # Même format collé : 'AALEXIS HOULE' -> 'ALEXIS HOULE'
                name_clean = re.sub(r'^(.)\1+', r'\1', raw_name.strip(), count=1)
                name_clean = re.sub(r'\s+', ' ', name_clean).strip()
                if name_clean:
                    personnel.append({"nom": name_clean, "role": clean_role(role)})

    print(f"[PARSING ROSTER] {len(gardiens)} gardien(s), {len(joueurs)} joueur(s), {len(personnel)} membre(s) du personnel.")
    return {"gardiens": gardiens, "joueurs": joueurs, "personnel": personnel}


# ---------- PARSING SCHEDULE ----------

def parse_schedule_structured(html: str):
    try:
        with open("/data/schedule_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Regex dates plus permissive (jours optionnels, abréviations, etc.)
    date_pattern = (
        r'((?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?'
        r'(\d{1,2})\s+'
        r'(janv\.|janvier|févr\.|février|mars|avr\.|avril|mai|juin|juill\.|juillet|'
        r'août|sept\.|septembre|oct\.|octobre|nov\.|novembre|déc\.|décembre)\s+'
        r'(\d{4})'
    )
    matches = re.findall(date_pattern, text, re.I)
    unique_dates = []
    seen = set()
    for m in matches:
        full = " ".join(p for p in m if p).strip()
        if full and full not in seen:
            seen.add(full)
            unique_dates.append(full)

    # Essayer aussi d'extraire les matchs avec équipes (format "Équipe A vs Équipe B")
    match_pattern = re.compile(
        r'(\d{1,2}\s+(?:janv\.|févr\.|mars|avr\.|mai|juin|juill\.|août|sept\.|oct\.|nov\.|déc\.|'
        r'janvier|février|avril|juillet|septembre|octobre|novembre|décembre)\s+\d{4})'
        r'[^\d]{0,80}?'
        r'(\d{1,2}:\d{2})',
        re.I
    )
    games = [{"date": d, "heure": h} for d, h in match_pattern.findall(text)]

    print(f"[PARSING SCHEDULE] {len(unique_dates)} date(s) unique(s), {len(games)} match(s) avec heure.")
    return {
        "dates_trouvees": unique_dates,
        "matchs": games,
        "prochains_matchs": games if games else [{"date": d} for d in unique_dates]
    }


# ---------- PARSING STATS ----------

def parse_table_generic(html: str, debug_name: str = "generic"):
    try:
        with open(f"/data/{debug_name}_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    headers = [th.get_text(strip=True) for th in table.select("thead th")]
    rows = []
    for tr in table.select("tbody tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= len(headers):
            rows.append(dict(zip(headers, tds)))
    print(f"[PARSING {debug_name.upper()}] {len(rows)} ligne(s) | En-têtes: {headers}")
    return rows


# ---------- PARSING STANDINGS ----------

def parse_standings(html: str, category_uuid: str = None):
    """
    Récupère le classement de la bonne catégorie.
    On essaie de trouver la table dont les lignes contiennent MY_TEAM_NAME.
    """
    try:
        with open("/data/standings_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    all_rows = []
    best_table_rows = []

    for t_idx, table in enumerate(soup.find_all("table")):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if "Équipe" not in headers:
            continue
        rows = []
        for tr in table.select("tbody tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= len(headers):
                rows.append(dict(zip(headers, tds)))
        if rows:
            all_rows.extend(rows)
            # Détecter la table contenant notre équipe
            for r in rows:
                if MY_TEAM_NAME in r.get("Équipe", "").upper():
                    best_table_rows = rows
                    break

    return all_rows, best_table_rows


# ---------- SCRAPING PRINCIPAL ----------

async def scrape_and_publish():
    print(f"\n==================================================")
    print(f"[START] Scraping RseqHockey v2.1.0 (FlareSolverr) - Équipe: {MY_TEAM_NAME}")
    print(f"==================================================")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    urls = {
        "roster": f"{team_url}?tab=roster",
        "schedule": f"{team_url}?tab=schedule",
        "standings": f"{team_url}?tab=standings",
        "stats_saison_reguliere": f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
    }

    scraped_html = {}
    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key, url in urls.items():
            html = await fetch_with_flaresolverr(session, url, key)
            scraped_html[key] = html
            if html is None:
                print(f"[ABORT] Impossible de récupérer '{key}'.")
                return

    print("\n[PARSING] Traitement des données extraites...")
    roster_data = parse_roster_structured(scraped_html["roster"])
    schedule_data = parse_schedule_structured(scraped_html["schedule"])
    player_stats_sr = parse_table_generic(scraped_html["stats_saison_reguliere"], "stats_saison_reguliere")
    standings_all, standings_my_team = parse_standings(scraped_html["standings"])

    my_team_info = next((t for t in standings_all if MY_TEAM_NAME in t.get("Équipe", "").upper()), None)
    player_info_sr = next((p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()), None) if TRACKED_PLAYER else None

    print("\n[MQTT] Publication...")

    def build_config(name, object_id, state_topic, attr_topic, icon):
        return {
            "name": name,
            "object_id": object_id,
            "unique_id": object_id,
            "state_topic": state_topic,
            "json_attributes_topic": attr_topic,
            "icon": icon,
            "device": DEVICE_INFO
        }

    def publish_config(topic, cfg):
        print(f"\n[MQTT DISCOVERY] {topic}")
        publish_to_mqtt(topic, cfg)

    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])

    # --- Roster ---
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Alignement {TEAM_ID}", f"rseqhockey_{TEAM_ID}_roster",
                     f"rseqhockey/{TEAM_ID}/roster/state",
                     f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group")
    )
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state",
                    f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    # --- Horaire ---
    prochain_match = schedule_data["prochains_matchs"][0]["date"] if schedule_data["prochains_matchs"] else "Aucun match prévu"
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_schedule/config",
        build_config(f"RSEQ Horaire {TEAM_ID}", f"rseqhockey_{TEAM_ID}_schedule",
                     f"rseqhockey/{TEAM_ID}/schedule/state",
                     f"rseqhockey/{TEAM_ID}/schedule/attributes", "mdi:calendar-clock")
    )
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/state", prochain_match)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/attributes", schedule_data)

    # --- Classement ---
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config",
        build_config(f"RSEQ Classement {MY_TEAM_NAME.capitalize()}", f"rseqhockey_{TEAM_ID}_standings",
                     f"rseqhockey/{TEAM_ID}/standings/state",
                     f"rseqhockey/{TEAM_ID}/standings/attributes", "mdi:trophy")
    )
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state",
                    f"{my_team_info.get('#', 'N/A')}e rang" if my_team_info else "Saison en cours")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes",
                    {"mon_equipe": my_team_info, "classement_complet": standings_all})

    # --- Joueur suivi ---
    if TRACKED_PLAYER:
        player_slug = re.sub(r'[^a-zA-Z0-9]', '_', TRACKED_PLAYER)
        pts_sr = player_info_sr.get("PTS", "0") if player_info_sr else "0"
        publish_config(
            f"homeassistant/sensor/rseqhockey_player_{player_slug}/config",
            build_config(f"RSEQ Joueur {TRACKED_PLAYER.capitalize()}", f"rseqhockey_player_{player_slug}",
                         f"rseqhockey/player/{player_slug}/state",
                         f"rseqhockey/player/{player_slug}/attributes", "mdi:account-star")
        )
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/state", f"SR: {pts_sr} pts")
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/attributes", {"stats_saison_reguliere": player_info_sr})

    # --- Stats saison régulière ---
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_stats_saison_reguliere/config",
        build_config(f"RSEQ Stats Saison {TEAM_ID}", f"rseqhockey_{TEAM_ID}_stats_saison_reguliere",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", "mdi:hockey-sticks")
    )
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state", f"{len(player_stats_sr)} joueurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", {"joueurs": player_stats_sr})

    print("\n[FIN] Cycle terminé.")


if __name__ == "__main__":
    while True:
        asyncio.run(scrape_and_publish())
        print(f"\n[ATTENTE] Prochaine mise à jour dans {INTERVAL / 3600} heures...")
        time.sleep(INTERVAL)
