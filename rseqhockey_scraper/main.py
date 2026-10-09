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

# --- FlareSolverr ---
FLARESOLVERR_URL = config.get("flaresolverr_url", "http://192.168.2.65:8191/v1")
FLARESOLVERR_TIMEOUT = config.get("flaresolverr_timeout", 90)  # en secondes

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
    """Récupère le HTML d'une URL via FlareSolverr (bypass Cloudflare)."""
    print(f"\n[FLARESOLVERR] Requête '{key_name}': {url}")
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FLARESOLVERR_TIMEOUT * 1000
    }
    try:
        async with session.post(FLARESOLVERR_URL, json=payload) as resp:
            data = await resp.json()
    except Exception as e:
        print(f"[FLARESOLVERR ERROR '{key_name}'] Requête HTTP échouée: {e}")
        return None

    status = data.get("status")
    if status != "ok":
        print(f"[FLARESOLVERR ERROR '{key_name}'] Status: {status} | Message: {data.get('message')}")
        return None

    solution = data.get("solution", {})
    html = solution.get("response", "")
    if not html:
        print(f"[FLARESOLVERR ERROR '{key_name}'] Réponse vide.")
        return None

    # Vérification défensive : est-ce que Cloudflare est encore dans la page ?
    lower = html.lower()
    if "vérification de sécurité en cours" in lower or "just a moment" in lower:
        print(f"[FLARESOLVERR WARN '{key_name}'] Page challenge détectée malgré status ok.")
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
        client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)
        client.disconnect()
        print(f"[MQTT SUCCESS] Publié sur: {topic}")
    except Exception as e:
        print(f"[MQTT ERROR] Échec sur {topic}: {e}")


# ---------- PARSING ----------

def clean_player_name(name: str) -> str:
    name = name.strip()
    if len(name) > 1 and name[0] == name[1] and name[0].isupper():
        name = name[1:]
    return name


def parse_roster_structured(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    print(f"\n--- [DEBUG ROSTER] ---")
    print(f"Longueur HTML: {len(html)} octets")
    print(f"Nombre de balises <table>: {len(soup.find_all('table'))}")
    print(f"Extrait du texte capturé (300 chars):\n{text[:300]}")
    print(f"----------------------\n")

    gardiens = []
    joueurs = []
    personnel = []

    tables = soup.find_all("table")
    for t_idx, table in enumerate(tables):
        rows = table.select("tbody tr")
        print(f"[DEBUG ROSTER TABLE #{t_idx}] {len(rows)} ligne(s) dans le tableau")
        for tr in rows:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 2:
                raw_name = tds[0]
                num = tds[1] if tds[1].isdigit() else (tds[2] if len(tds) > 2 and tds[2].isdigit() else "")
                pos = tds[-1].upper() if len(tds) >= 3 else "F"
                clean_n = clean_player_name(raw_name)
                item = {"nom": clean_n, "numero": num, "position": pos}

                if pos in ["G", "GK", "GARDIEN"]:
                    if not any(g["nom"] == clean_n for g in gardiens):
                        gardiens.append(item)
                else:
                    if not any(j["nom"] == clean_n for j in joueurs):
                        joueurs.append(item)

    if not joueurs and not gardiens:
        staff_matches = re.findall(r'([A-Za-zÀ-ÖØ-öø-ÿ\s\-]{3,30}?)\s*(Entraîneur-Adjoint|Entraîneur-Chef|Entraîneur|Gérant|Préposé)', text)
        for raw_name, role in staff_matches:
            clean_n = clean_player_name(raw_name)
            if clean_n and not any(p["nom"] == clean_n for p in personnel):
                personnel.append({"nom": clean_n, "role": role})

        player_matches = re.findall(r'([A-Za-zÀ-ÖØ-öø-ÿ\s\-]{3,30}?)\s*(\d{1,2})\s*([FGD]|DG|AG|AD|Gardiens?|Joueurs?)\b', text)
        for raw_name, num, pos in player_matches:
            words = [w for w in raw_name.split() if w.upper() not in ["POSITION", "GARDIENS", "JOUEURS", "PERSONNEL", "DE", "L'ÉQUIPE", "POS"]]
            if not words:
                continue
            full_name = clean_player_name(" ".join(words))
            item = {"nom": full_name, "numero": num, "position": pos}
            if pos.upper() in ["G", "GARDIEN"] or "GARD" in pos.upper():
                if not any(g["nom"] == full_name for g in gardiens):
                    gardiens.append(item)
            else:
                if not any(j["nom"] == full_name for j in joueurs):
                    joueurs.append(item)

    print(f"[PARSING ROSTER] {len(gardiens)} gardien(s), {len(joueurs)} joueur(s), {len(personnel)} membre(s) du personnel trouvés.")
    return {"gardiens": gardiens, "joueurs": joueurs, "personnel": personnel}


def parse_schedule_structured(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    print(f"\n--- [DEBUG SCHEDULE] ---")
    print(f"Longueur HTML: {len(html)} octets")
    print(f"Extrait du texte capturé (300 chars):\n{text[:300]}")
    print(f"------------------------\n")

    dates = re.findall(r'((?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*\d{1,2}\s+(?:janv\.|févr\.|mars|avr\.|mai|juin|juill\.|août|sept\.|oct\.|nov\.|déc\.|janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4})', text, re.I)
    unique_dates = list(dict.fromkeys([d.strip() for d in dates if len(d.strip()) > 5]))

    events = [{"date": d, "description": "Match au calendrier"} for d in unique_dates]
    print(f"[PARSING SCHEDULE] {len(unique_dates)} date(s) de match trouvée(s).")
    return {"dates_trouvees": unique_dates, "prochains_matchs": events}


def parse_table_generic(html: str):
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
    return rows


def parse_standings(html: str):
    soup = BeautifulSoup(html, "html.parser")
    all_rows = []
    tables = soup.find_all("table")
    for table in tables:
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        for tr in table.select("tbody tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= len(headers):
                row = dict(zip(headers, tds))
                all_rows.append(row)
    return all_rows


# ---------- SCRAPING PRINCIPAL ----------

async def scrape_and_publish():
    print(f"\n==================================================")
    print(f"[START] Scraping RseqHockey v2.0.0 (FlareSolverr) - Équipe: {MY_TEAM_NAME}")
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

    # Timeout global côté client aiohttp (le vrai timeout est géré par FlareSolverr)
    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key, url in urls.items():
            html = await fetch_with_flaresolverr(session, url, key)
            scraped_html[key] = html
            if html is None:
                print(f"[ABORT] Impossible de récupérer '{key}', arrêt du cycle pour ne pas publier de données incomplètes.")
                return

    print("\n[PARSING] Traitement des données extraites...")
    roster_data = parse_roster_structured(scraped_html["roster"])
    schedule_data = parse_schedule_structured(scraped_html["schedule"])
    player_stats_sr = parse_table_generic(scraped_html["stats_saison_reguliere"])
    standings_list = parse_standings(scraped_html["standings"])

    my_team_info = next((t for t in standings_list if MY_TEAM_NAME in t.get("Équipe", "").upper()), None)
    player_info_sr = next((p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()), None) if TRACKED_PLAYER else None

    print("\n[MQTT] Début de la publication sur le broker...")

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

    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])
    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Hockey Équipe {TEAM_ID} Alignement", f"rseqhockey_{TEAM_ID}_roster", f"rseqhockey/{TEAM_ID}/roster/state", f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state", f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    prochain_match = schedule_data["dates_trouvees"][0] if schedule_data["dates_trouvees"] else "Aucun match prévu"
    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_schedule/config",
        build_config(f"RSEQ Hockey Équipe {TEAM_ID} Horaire", f"rseqhockey_{TEAM_ID}_schedule", f"rseqhockey/{TEAM_ID}/schedule/state", f"rseqhockey/{TEAM_ID}/schedule/attributes", "mdi:calendar-clock"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/state", prochain_match)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/attributes", schedule_data)

    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config",
        build_config(f"RSEQ Hockey Classement {MY_TEAM_NAME.capitalize()}", f"rseqhockey_{TEAM_ID}_standings", f"rseqhockey/{TEAM_ID}/standings/state", f"rseqhockey/{TEAM_ID}/standings/attributes", "mdi:trophy"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state", f"{my_team_info.get('#', 'N/A')}e rang" if my_team_info else "Saison en cours")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes", {"mon_equipe": my_team_info, "classement_complet": standings_list})

    if TRACKED_PLAYER:
        player_slug = re.sub(r'[^a-zA-Z0-9]', '_', TRACKED_PLAYER)
        pts_sr = player_info_sr.get("PTS", "0") if player_info_sr else "0"
        publish_to_mqtt(f"homeassistant/sensor/rseqhockey_player_{player_slug}/config",
            build_config(f"RSEQ Hockey Joueur {TRACKED_PLAYER.capitalize()}", f"rseqhockey_player_{player_slug}", f"rseqhockey/player/{player_slug}/state", f"rseqhockey/player/{player_slug}/attributes", "mdi:account-star"))
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/state", f"SR: {pts_sr} pts")
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/attributes", {"stats_saison_reguliere": player_info_sr})

    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_stats_saison_reguliere/config",
        build_config(f"RSEQ Hockey Équipe {TEAM_ID} Stats Saison Reguliere", f"rseqhockey_{TEAM_ID}_stats_saison_reguliere", f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state", f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", "mdi:hockey-sticks"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state", f"{len(player_stats_sr)} joueurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", {"joueurs": player_stats_sr})

    print("\n[FIN] Cycle terminé avec succès.")


if __name__ == "__main__":
    while True:
        asyncio.run(scrape_and_publish())
        print(f"\n[ATTENTE] Prochaine mise à jour dans {INTERVAL / 3600} heures...")
        time.sleep(INTERVAL)
