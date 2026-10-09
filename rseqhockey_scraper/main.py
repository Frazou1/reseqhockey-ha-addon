import json
import time
import re
import os
import urllib.request
import asyncio
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
from playwright.async_api import async_playwright

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

LEAGUE_UUID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SCHEDULE_HORS_CONCOURS = ""
SCHEDULE_SAISON_REGULIERE = "198862"

LOGOS_DIR = "/share/rseqhockey_logos"
WWW_LOGOS_DIR = "/config/www/rseqhockey_logos"
os.makedirs(LOGOS_DIR, exist_ok=True)

if os.path.exists("/config/www") and not os.path.exists(WWW_LOGOS_DIR):
    try:
        os.symlink(LOGOS_DIR, WWW_LOGOS_DIR)
    except Exception as e:
        print(f"[LOGOS Note] {e}")

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

def clean_player_name(name: str) -> str:
    name = name.strip()
    if len(name) > 1 and name[0] == name[1] and name[0].isupper():
        name = name[1:]
    return name

def parse_roster_structured(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    
    gardiens = []
    joueurs = []
    personnel = []

    staff_patterns = re.findall(r'([A-Za-zÀ-ÖØ-öø-ÿ\s\-]+?)\s*(Entraîneur-Adjoint|Entraîneur-Chef|Entraîneur|Gérant|Préposé)', text)
    for raw_name, role in staff_patterns:
        words = [w for w in raw_name.split() if w.upper() not in ["POSITION", "PERSONNEL", "DE", "L'ÉQUIPE"]]
        if words:
            clean_n = clean_player_name(" ".join(words[-2:]))
            if clean_n and not any(p["nom"] == clean_n for p in personnel):
                personnel.append({"nom": clean_n, "role": role})

    player_matches = re.findall(r'([A-Za-zÀ-ÖØ-öø-ÿ\s\-]{3,30}?)\s*(\d{1,2})\s*([FGD]|DG|AG|AD|Gardiens?|Joueurs?)\b', text)
    
    for raw_name, num, pos in player_matches:
        words = [w for w in raw_name.split() if w.upper() not in ["POSITION", "GARDIENS", "JOUEURS", "PERSONNEL", "DE", "L'ÉQUIPE"]]
        if not words:
            continue
        full_name = clean_player_name(" ".join(words))
        
        item = {"nom": full_name, "numero": num, "position": pos}
        if pos.upper() in ["G", "GARDIEN", "GARDIENS"] or "GARD" in pos.upper():
            if not any(g["nom"] == full_name for g in gardiens):
                gardiens.append(item)
        else:
            if not any(j["nom"] == full_name for j in joueurs):
                joueurs.append(item)

    if not joueurs and not gardiens:
        raw_items = re.findall(r'([A-Z][A-Z\s\-]+)(\d{1,2})([A-Z]{1,2})', text)
        for raw_name, num, pos in raw_items:
            clean_n = clean_player_name(raw_name)
            item = {"nom": clean_n, "numero": num, "position": pos}
            if pos == "G":
                gardiens.append(item)
            else:
                joueurs.append(item)

    print(f"[PARSING ROSTER] {len(gardiens)} gardien(s), {len(joueurs)} joueur(s), {len(personnel)} membre(s) du personnel trouvés.")
    return {"gardiens": gardiens, "joueurs": joueurs, "personnel": personnel}

def parse_schedule_structured(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    
    dates = re.findall(r'((?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+\d{1,2}\s+[a-zà-û]+\s+\d{4})', text, re.I)
    unique_dates = list(dict.fromkeys(dates))
    
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
                img = tr.find("img")
                if img and img.get("src"):
                    team_name = row.get("Équipe", "").strip()
                    clean_filename = re.sub(r'[^a-zA-Z0-9]', '_', team_name).lower() + ".png"
                    row["logo_url"] = f"/local/rseqhockey_logos/{clean_filename}"
                all_rows.append(row)
    return all_rows

def download_logos(soup: BeautifulSoup):
    images = soup.find_all("img")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}
    for img in images:
        src = img.get("src", "")
        alt = img.get("alt", "").strip()
        if src.startswith("http") and ("team" in src or "logo" in src or len(alt) > 2):
            clean_name = re.sub(r'[^a-zA-Z0-9]', '_', alt if alt else src.split("/")[-1]).lower()
            file_path = os.path.join(LOGOS_DIR, f"{clean_name}.png")
            if not os.path.exists(file_path):
                try:
                    req = urllib.request.Request(src, headers=headers)
                    with urllib.request.urlopen(req) as response, open(file_path, 'wb') as out_file:
                        out_file.write(response.read())
                except Exception:
                    pass

async def scrape_and_publish():
    print(f"\n==================================================")
    print(f"[START] Scraping RseqHockey v1.4.2 (Équipe: {MY_TEAM_NAME})")
    print(f"==================================================")
    
    base_url = f"https://scolaire.rseqhockey.com/fr/-rseq/schedule-stats-standings/{LEAGUE_UUID}"
    
    urls_to_scrape = {
        "roster": f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}",
        "schedule": f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}?tab=schedule",
        "standings": f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}?tab=standings",
        "stats_saison_reguliere": f"{base_url}?scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats"
    }
    
    if SCHEDULE_HORS_CONCOURS:
        urls_to_scrape["stats_hors_concours"] = f"{base_url}?scheduleId={SCHEDULE_HORS_CONCOURS}&tab=playerstats"

    scraped_html = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            print("[SESSION] Initialisation de la session RseqHockey...")
            await page.goto("https://scolaire.rseqhockey.com/fr/-rseq", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            cookie_btn = page.locator("button:has-text('Accepter')").or_(page.locator("button:has-text('Accept')"))
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
                print("[SESSION] Cookies acceptés.")
                await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[SESSION NOTE] {e}")

        for key, url in urls_to_scrape.items():
            try:
                print(f"[SCRAPE] Chargement de '{key}'...")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                if key == "roster":
                    try:
                        await page.wait_for_selector("text=Position", timeout=8000)
                    except Exception:
                        print("[ROSTER RETRY] Attente du texte Position dépassée, tentative de scroll...")
                
                await page.wait_for_timeout(3500)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)

                html_content = await page.content()
                scraped_html[key] = html_content
                download_logos(BeautifulSoup(html_content, "html.parser"))
            except Exception as e:
                print(f"[SCRAPE ERROR] Échec sur {key}: {e}")
                scraped_html[key] = ""

        await browser.close()

    print("\n[PARSING] Traitement des données extraites...")
    roster_data = parse_roster_structured(scraped_html.get("roster", ""))
    schedule_data = parse_schedule_structured(scraped_html.get("schedule", ""))
    player_stats_hc = parse_table_generic(scraped_html.get("stats_hors_concours", "")) if SCHEDULE_HORS_CONCOURS else []
    player_stats_sr = parse_table_generic(scraped_html.get("stats_saison_reguliere", ""))
    standings_list = parse_standings(scraped_html.get("standings", ""))

    my_team_info = next((t for t in standings_list if MY_TEAM_NAME in t.get("Équipe", "").upper()), None)
    player_info_hc = next((p for p in player_stats_hc if TRACKED_PLAYER in p.get("Nom", "").lower()), None) if TRACKED_PLAYER and player_stats_hc else None
    player_info_sr = next((p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()), None) if TRACKED_PLAYER else None

    print("\n[MQTT] Début de la publication sur le broker...")

    # 1. Alignement (Roster)
    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])
    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config", {
        "name": f"RSEQ Hockey Équipe {TEAM_ID} Alignement",
        "unique_id": f"rseqhockey_team_{TEAM_ID}_roster",
        "state_topic": f"rseqhockey/{TEAM_ID}/roster/state",
        "json_attributes_topic": f"rseqhockey/{TEAM_ID}/roster/attributes",
        "icon": "mdi:account-group"
    })
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state", f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    # 2. Horaire (Schedule)
    prochain_match = schedule_data["dates_trouvees"][0] if schedule_data["dates_trouvees"] else "Aucun match prévu"
    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_schedule/config", {
        "name": f"RSEQ Hockey Équipe {TEAM_ID} Horaire",
        "unique_id": f"rseqhockey_team_{TEAM_ID}_schedule",
        "state_topic": f"rseqhockey/{TEAM_ID}/schedule/state",
        "json_attributes_topic": f"rseqhockey/{TEAM_ID}/schedule/attributes",
        "icon": "mdi:calendar-clock"
    })
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/state", prochain_match)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/attributes", schedule_data)

    # 3. Classement
    publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config", {
        "name": f"RSEQ Hockey Classement {MY_TEAM_NAME.capitalize()}",
        "unique_id": f"rseqhockey_team_{TEAM_ID}_standings",
        "state_topic": f"rseqhockey/{TEAM_ID}/standings/state",
        "json_attributes_topic": f"rseqhockey/{TEAM_ID}/standings/attributes",
        "icon": "mdi:trophy"
    })
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state", f"{my_team_info.get('#', 'N/A')}e rang" if my_team_info else "Saison en cours")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes", {"mon_equipe": my_team_info, "classement_complet": standings_list})

    # 4. Joueur Suivi
    if TRACKED_PLAYER:
        player_slug = re.sub(r'[^a-zA-Z0-9]', '_', TRACKED_PLAYER)
        pts_hc = player_info_hc.get("PTS", "0") if player_info_hc else "0"
        pts_sr = player_info_sr.get("PTS", "0") if player_info_sr else "0"
        
        publish_to_mqtt(f"homeassistant/sensor/rseqhockey_player_{player_slug}/config", {
            "name": f"RSEQ Hockey Joueur {TRACKED_PLAYER.capitalize()}",
            "unique_id": f"rseqhockey_player_{player_slug}",
            "state_topic": f"rseqhockey/player/{player_slug}/state",
            "json_attributes_topic": f"rseqhockey/player/{player_slug}/attributes",
            "icon": "mdi:account-star"
        })
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/state", f"SR: {pts_sr} pts | HC: {pts_hc} pts")
        publish_to_mqtt(f"rseqhockey/player/{player_slug}/attributes", {"stats_saison_reguliere": player_info_sr, "stats_hors_concours": player_info_hc})

    # 5. Stats d'Équipe (Saison Régulière & Hors-Concours si présente)
    stats_to_publish = [("stats_saison_reguliere", player_stats_sr)]
    if SCHEDULE_HORS_CONCOURS:
        stats_to_publish.append(("stats_hors_concours", player_stats_hc))

    for key, data_list in stats_to_publish:
        publish_to_mqtt(f"homeassistant/sensor/rseqhockey_{TEAM_ID}_{key}/config", {
            "name": f"RSEQ Hockey Équipe {TEAM_ID} {key.replace('_', ' ').capitalize()}",
            "unique_id": f"rseqhockey_team_{TEAM_ID}_{key}",
            "state_topic": f"rseqhockey/{TEAM_ID}/{key}/state",
            "json_attributes_topic": f"rseqhockey/{TEAM_ID}/{key}/attributes",
            "icon": "mdi:hockey-sticks"
        })
        publish_to_mqtt(f"rseqhockey/{TEAM_ID}/{key}/state", f"{len(data_list)} joueurs")
        publish_to_mqtt(f"rseqhockey/{TEAM_ID}/{key}/attributes", {"joueurs": data_list})

    print("\n[FIN] Cycle terminé avec succès.")

if __name__ == "__main__":
    while True:
        asyncio.run(scrape_and_publish())
        print(f"\n[ATTENTE] Prochaine mise à jour dans {INTERVAL / 3600} heures...")
        time.sleep(INTERVAL)
