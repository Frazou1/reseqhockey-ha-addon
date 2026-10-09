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
INTERVAL = config.get("update_interval_hours", 1) * 3600  # défaut 1h pour garder le cookie frais

LEAGUE_UUID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SCHEDULE_SAISON_REGULIERE = "198862"

LOGOS_DIR = "/share/rseqhockey_logos"
WWW_LOGOS_DIR = "/config/www/rseqhockey_logos"
os.makedirs(LOGOS_DIR, exist_ok=True)

# Profil persistant Playwright (garde les cookies Cloudflare entre les runs)
USER_DATA_DIR = "/data/playwright_profile"
os.makedirs(USER_DATA_DIR, exist_ok=True)

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

async def wait_for_cloudflare(page, timeout_sec=40):
    """Détecte la fin du challenge Cloudflare en vérifiant le VRAI contenu (tables)."""
    for i in range(timeout_sec):
        try:
            title = (await page.title()).lower()
            content = (await page.content()).lower()
        except Exception:
            await page.wait_for_timeout(1000)
            continue

        # Indices que Cloudflare est ENCORE actif
        is_cf_active = (
            any(term in title for term in ["just a moment", "un instant"]) or
            "vérification de sécurité en cours" in content or
            "verification de securite en cours" in content or
            "cf-mitigated" in content or
            "challenge-platform" in content
        )

        # Indice qu'on a le VRAI contenu
        try:
            has_real_content = (await page.locator("table, .spordle-container, div.team-roster, div.schedule-container").count()) > 0
        except Exception:
            has_real_content = False

        if not is_cf_active and has_real_content:
            real_title = await page.title()
            print(f"[CLOUDFLARE BYPASS] Validé après {i+1}s (Titre: '{real_title}')")
            return True

        # Si après 10s Cloudflare semble inactif mais aucun contenu réel => blocage silencieux
        if not is_cf_active and not has_real_content and i >= 10:
            print(f"[CLOUDFLARE FAIL] Challenge 'passé' mais contenu réel absent après {i+1}s.")
            return False

        if (i + 1) % 5 == 0:
            print(f"[CLOUDFLARE] Vérification en cours ({i+1}s)...")
        await page.wait_for_timeout(1000)

    print("[CLOUDFLARE WARN] Défi non franchi dans le délai imparti.")
    return False

async def load_page_with_debug(page, url, key_name):
    print(f"\n[SCRAPE] Navigation vers '{key_name}': {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[SCRAPE ERROR '{key_name}'] Navigation échouée: {e}")
        return None

    if not await wait_for_cloudflare(page):
        return None

    try:
        await page.wait_for_selector("table, div.spordle-container, div.team-roster, div.schedule-container", timeout=10000)
        print(f"[SCRAPE DEBUG '{key_name}'] Composant DOM détecté !")
    except Exception:
        print(f"[SCRAPE DEBUG '{key_name}'] Aucun sélecteur spécifique, poursuite...")

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)

    content = await page.content()
    title = await page.title()
    print(f"[SCRAPE OK '{key_name}'] Titre: '{title}' | Taille HTML: {len(content)} octets")
    return content

async def scrape_and_publish():
    print(f"\n==================================================")
    print(f"[START] Scraping RseqHockey v1.7.0 (Équipe: {MY_TEAM_NAME})")
    print(f"==================================================")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    scraped_html = {}
    context = None

    try:
        async with async_playwright() as p:
            # --- PROFIL PERSISTANT : garde les cookies Cloudflare entre les runs ---
            print(f"[SESSION] Utilisation du profil persistant: {USER_DATA_DIR}")
            context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=True,
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="fr-CA",
                timezone_id="America/Toronto",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--window-size=1280,900",
                ],
            )

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['fr-CA', 'fr', 'en-US', 'en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                window.chrome = { runtime: {} };
            """)

            page = context.pages[0] if context.pages else await context.new_page()

            print("[SESSION] Connexion initiale à la plateforme RSEQ...")
            try:
                await page.goto("https://scolaire.rseqhockey.com/fr", wait_until="domcontentloaded", timeout=45000)
                await wait_for_cloudflare(page)

                # Accepter les cookies si la bannière apparaît
                cookie_btn = page.locator("button:has-text('Accepter')").or_(page.locator("button:has-text('Accept')"))
                if await cookie_btn.count() > 0:
                    await cookie_btn.first.click()
                    print("[SESSION] Cookies acceptés.")
                    await page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[SESSION NOTE] {e}")

            scraped_html["roster"] = await load_page_with_debug(page, f"{team_url}?tab=roster", "roster")
            scraped_html["schedule"] = await load_page_with_debug(page, f"{team_url}?tab=schedule", "schedule")
            scraped_html["standings"] = await load_page_with_debug(page, f"{team_url}?tab=standings", "standings")

            stats_url = f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats"
            scraped_html["stats_saison_reguliere"] = await load_page_with_debug(page, stats_url, "stats_saison_reguliere")

    except Exception as e:
        print(f"[FATAL] Erreur pendant le scraping: {e}")
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    # --- Vérification : si une page est None, Cloudflare a bloqué ---
    failed = [k for k, v in scraped_html.items() if not v]
    if failed:
        print(f"\n[ABORT] Cloudflare a bloqué les pages suivantes: {failed}")
        print("[ABORT] Aucune publication MQTT (pour éviter d'écraser les données valides).")
        return

    print("\n[PARSING] Traitement des données extraites...")
    roster_data = parse_roster_structured(scraped_html.get("roster", ""))
    schedule_data = parse_schedule_structured(scraped_html.get("schedule", ""))
    player_stats_sr = parse_table_generic(scraped_html.get("stats_saison_reguliere", ""))
    standings_list = parse_standings(scraped_html.get("standings", ""))

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
