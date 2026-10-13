import json
import time
import re
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
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


# ---------- PLAYWRIGHT : clics automatisés sur l'horaire ----------

async def fetch_schedule_with_clicks(team_url):
    """
    Ouvre la page horaire, clique sur 'Saison 2026-27' puis 'Appliquer',
    et retourne le HTML une fois la liste des matchs chargée.
    """
    print(f"\n[PLAYWRIGHT] Démarrage du navigateur pour l'horaire...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="fr-CA",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
        """)

        page = await context.new_page()

        url = f"{team_url}?tab=schedule"
        print(f"[PLAYWRIGHT] Navigation vers {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[PLAYWRIGHT ERROR] Navigation: {e}")
            await browser.close()
            return None

        # Attendre que la page soit prête (Cloudflare inclus)
        print("[PLAYWRIGHT] Attente de la page...")
        for i in range(40):
            title = (await page.title()).lower()
            if "un instant" not in title and "just a moment" not in title:
                break
            await page.wait_for_timeout(1000)
        print(f"[PLAYWRIGHT] Titre: '{await page.title()}'")

        # Attendre que le contenu de l'horaire soit là
        try:
            await page.wait_for_selector("text=7 prochains jours", timeout=20000)
            print("[PLAYWRIGHT] Bouton '7 prochains jours' détecté.")
        except Exception as e:
            print(f"[PLAYWRIGHT WARN] Bouton filtre non trouvé: {e}")

        # Étape 1 : cliquer sur le bouton de filtre (dropdown)
        try:
            # Le bouton contient le texte "7 prochains jours"
            await page.locator("button:has-text('prochains jours'), button:has-text('prochain jour')").first.click(timeout=10000)
            print("[PLAYWRIGHT] Dropdown du filtre ouvert.")
            await page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[PLAYWRIGHT WARN] Impossible d'ouvrir le dropdown: {e}")

        # Étape 2 : cliquer sur "Saison 2026-27"
        try:
            await page.locator("li:has-text('Saison 2026-27')").first.click(timeout=10000)
            print("[PLAYWRIGHT] 'Saison 2026-27' cliqué.")
            await page.wait_for_timeout(800)
        except Exception as e:
            print(f"[PLAYWRIGHT WARN] 'Saison 2026-27' non cliqué: {e}")

        # Étape 3 : cliquer sur "Appliquer"
        try:
            await page.locator("button:has-text('Appliquer')").first.click(timeout=10000)
            print("[PLAYWRIGHT] 'Appliquer' cliqué.")
        except Exception as e:
            print(f"[PLAYWRIGHT WARN] 'Appliquer' non cliqué: {e}")

        # Attendre le rechargement des matchs
        print("[PLAYWRIGHT] Attente du chargement des matchs...")
        await page.wait_for_timeout(5000)

        # Attendre qu'il n'y ait plus de skeleton de chargement
        for _ in range(10):
            skeletons = await page.locator(".spdl-skeleton").count()
            if skeletons == 0:
                break
            await page.wait_for_timeout(1000)

        html = await page.content()
        print(f"[PLAYWRIGHT OK] HTML récupéré: {len(html)} octets")

        # Debug : sauvegarder
        try:
            with open("/share/schedule_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        await browser.close()
        return html


# ---------- FLARESOLVERR (pour roster + stats) ----------

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
        print(f"[FLARESOLVERR ERROR '{key_name}'] {data.get('message')}")
        return None
    html = data.get("solution", {}).get("response", "")
    if html:
        print(f"[FLARESOLVERR OK '{key_name}'] {len(html)} octets")
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
        client.disconnect()
        print(f"[MQTT SUCCESS] {topic}")
    except Exception as e:
        print(f"[MQTT ERROR] {topic}: {e}")


# ---------- PARSING SCHEDULE ----------

_JOURS_FR = ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"]
_MOIS_FR = ["JANV", "FÉVR", "MARS", "AVR", "MAI", "JUIN",
            "JUIL", "AOÛT", "SEPT", "OCT", "NOV", "DÉC"]


def _iso_to_date_fr(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto"))
        return f"{_JOURS_FR[dt.weekday()]}. {dt.day} {_MOIS_FR[dt.month-1]}.", dt.strftime("%Hh%M")
    except Exception:
        return iso, ""


def parse_schedule_cards(html: str):
    """Parse les cartes de match depuis le HTML rendu par Playwright."""
    soup = BeautifulSoup(html, "html.parser")
    matchs = []

    # Chaque match est dans un <article itemprop="item" itemtype=".../SportsEvent">
    articles = soup.find_all("article", attrs={"itemtype": re.compile("SportsEvent", re.I)})
    print(f"[SCHEDULE] {len(articles)} article(s) SportsEvent trouvé(s).")

    for art in articles:
        # Aréna : adresse dans le header
        arena = ""
        addr = art.find("address")
        if addr:
            arena = addr.get_text(" ", strip=True)

        # Numéro de match : <span class="EventCard_scheduleType...">ven. 2 oct. • P-2026</span>
        num_match = ""
        sched_type = art.find("span", class_=re.compile("scheduleType", re.I))
        if sched_type:
            txt = sched_type.get_text(" ", strip=True)
            m = re.search(r'P-?\d+', txt)
            if m:
                num_match = m.group(0)

        # Date/heure : <time datetime="..."> + <div class="font-brand font-size-lg">09 h 00</div>
        date_fr = ""
        heure_fr = ""
        iso = ""
        t = art.find("time")
        if t:
            iso = t.get("datetime", "")
            if iso:
                date_fr, heure_fr_from_iso = _iso_to_date_fr(iso)
        # Heure affichée dans le html
        heure_div = art.find("div", class_=re.compile("font-brand font-size-lg", re.I))
        if heure_div:
            heure_txt = heure_div.get_text(strip=True)
            m = re.match(r'(\d{1,2})\s*h\s*(\d{2})', heure_txt)
            if m:
                heure_fr = f"{m.group(1)}h{m.group(2)}"
        if not heure_fr and iso:
            _, heure_fr = _iso_to_date_fr(iso)

        # Équipes : chercher les 2 <article ...SportsTeam>
        teams = art.find_all("article", attrs={"itemtype": re.compile("SportsTeam", re.I)})
        equipes = []
        for t_art in teams:
            name_el = t_art.find(attrs={"itemprop": "name"})
            if name_el:
                a = name_el.find("a")
                if a:
                    equipes.append(a.get_text(strip=True))

        # Score : chercher "FINAL" ou les nombres à côté
        # Structure : <i class="mdi mdi-menu-right"></i>1<div>FINAL</div>0<i ...>
        score = ""
        status = ""
        final_div = art.find(string=re.compile(r'^\s*(FINAL|EN COURS|À VENIR)\s*$', re.I))
        if final_div:
            status = final_div.strip().upper()
            # Les scores sont dans les divs font-brand font-size-lg
            score_divs = art.find_all("div", class_=re.compile("font-brand font-size-lg", re.I))
            if len(score_divs) >= 2:
                # Le premier est le score équipe gauche, le dernier équipe droite
                s1 = score_divs[0].get_text(strip=True)
                s2 = score_divs[-1].get_text(strip=True)
                if s1.isdigit() and s2.isdigit():
                    score = f"{s1}-{s2}"
        else:
            # Pas de score => match à venir (structure avec time)
            status = "À VENIR"

        joue = bool(score)
        match = {
            "numero": num_match,
            "date": date_fr,
            "heure": heure_fr,
            "iso": iso,
            "arene": arena,
            "equipes": equipes,
            "score": score,
            "statut": status,
            "joue": joue,
        }
        matchs.append(match)
        print(f"   → {date_fr} {heure_fr} | {arena[:60]} | {equipes} | score={score or '-'} | {status}")

    return matchs


# ---------- PARSING ROSTER ----------

def split_name_and_number(raw: str):
    raw = raw.strip()
    if not raw:
        return "", ""
    num_match = re.search(r'(\d+)$', raw)
    numero = num_match.group(1) if num_match else ""
    name_part = raw[:num_match.start()] if num_match else raw
    name_part = re.sub(r'^(.)\1+', r'\1', name_part, count=1)
    parts = re.split(r'(?<=[a-zéèêëàâäîïôöùûüç])(?=[A-ZÉÈÊËÀÂÄÎÏÔÖÙÛÜÇ])', name_part)
    if len(parts) >= 2:
        full_name = f"{parts[0]} {' '.join(parts[1:])}"
    else:
        full_name = name_part
    return re.sub(r'\s+', ' ', full_name).strip(), numero


def parse_roster_structured(html: str):
    try:
        with open("/share/roster_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    gardiens, joueurs, personnel = [], [], []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not headers:
            continue
        hs = set(h.lower() for h in headers)
        if "gardiens" in hs:
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                nom, num = split_name_and_number(tds[0])
                if nom:
                    gardiens.append({"nom": nom, "numero": num, "stats": dict(zip(headers, tds))})
        elif "joueurs" in hs:
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                nom, num = split_name_and_number(tds[0])
                if nom:
                    joueurs.append({"nom": nom, "numero": num, "stats": dict(zip(headers, tds))})
        elif any("personnel" in h.lower() for h in headers):
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                nom = re.sub(r'^(.)\1+', r'\1', tds[0].strip(), count=1)
                nom = re.sub(r'\s+', ' ', nom).strip()
                if nom:
                    personnel.append({"nom": nom, "role": tds[1]})

    print(f"[PARSING ROSTER] {len(gardiens)} G, {len(joueurs)} J, {len(personnel)} P.")
    return {"gardiens": gardiens, "joueurs": joueurs, "personnel": personnel}


def parse_table_generic(html: str, debug_name: str = "generic"):
    try:
        with open(f"/share/{debug_name}_debug.html", "w", encoding="utf-8") as f:
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
    print(f"[PARSING {debug_name.upper()}] {len(rows)} ligne(s).")
    return rows


# ---------- MAIN ----------

async def scrape_and_publish():
    print(f"\n{'='*50}")
    print(f"[START] RseqHockey v2.7.0 (Playwright schedule) - {MY_TEAM_NAME}")
    print(f"{'='*50}")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    # --- 1. Horaire : Playwright avec clics ---
    schedule_html = await fetch_schedule_with_clicks(team_url)

    # --- 2. Roster + Stats : FlareSolverr ---
    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        roster_html = await fetch_with_flaresolverr(session, f"{team_url}?tab=roster", "roster")
        stats_html = await fetch_with_flaresolverr(
            session,
            f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
            "stats_saison_reguliere"
        )

    # --- PARSING ---
    print("\n[PARSING]...")
    matchs = parse_schedule_cards(schedule_html) if schedule_html else []
    roster_data = parse_roster_structured(roster_html) if roster_html else {"gardiens": [], "joueurs": [], "personnel": []}
    player_stats_sr = parse_table_generic(stats_html, "stats_saison_reguliere") if stats_html else []

    joues = [m for m in matchs if m["joue"]]
    a_venir = [m for m in matchs if not m["joue"]]

    # Trier par date ISO
    joues.sort(key=lambda m: m.get("iso", ""))
    a_venir.sort(key=lambda m: m.get("iso", ""))

    last_match = joues[-1] if joues else None
    next_match = a_venir[0] if a_venir else None

    print(f"[STATS] {len(joues)} joué(s), {len(a_venir)} à venir.")
    if last_match:
        print(f"   Dernier: {last_match['date']} {last_match['equipes']} {last_match['score']}")
    if next_match:
        print(f"   Prochain: {next_match['date']} {next_match['heure']} @ {next_match['arene']}")

    player_info_sr = next(
        (p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()),
        None
    ) if TRACKED_PLAYER else None

    # --- MQTT ---
    print("\n[MQTT] Publication...")

    def build_config(name, oid, st, at, icon):
        return {"name": name, "object_id": oid, "unique_id": oid,
                "state_topic": st, "json_attributes_topic": at,
                "icon": icon, "device": DEVICE_INFO}

    def publish_config(topic, cfg):
        print(f"\n[MQTT DISCOVERY] {topic}")
        publish_to_mqtt(topic, cfg)

    # Roster
    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Alignement", f"rseqhockey_{TEAM_ID}_roster",
                     f"rseqhockey/{TEAM_ID}/roster/state",
                     f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state",
                    f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    # Prochain match
    if next_match:
        state = f"{next_match['date']} {next_match['heure']} - {next_match['arene']}"
    else:
        state = "Aucun match prévu"
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_schedule/config",
        build_config(f"RSEQ Prochain match", f"rseqhockey_{TEAM_ID}_schedule",
                     f"rseqhockey/{TEAM_ID}/schedule/state",
                     f"rseqhockey/{TEAM_ID}/schedule/attributes", "mdi:calendar-clock"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/state", state)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/attributes", {
        "prochain_match": next_match,
        "matchs_a_venir": a_venir,
        "nb_total": len(matchs),
    })

    # Dernier match
    if last_match:
        if len(last_match["equipes"]) >= 2:
            last_state = f"{last_match['date']} : {' '.join(last_match['equipes'])} {last_match['score']}"
        else:
            last_state = f"{last_match['date']} | {last_match['score']}"
    else:
        last_state = "Aucun match joué"
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_last_match/config",
        build_config(f"RSEQ Dernier match", f"rseqhockey_{TEAM_ID}_last_match",
                     f"rseqhockey/{TEAM_ID}/last_match/state",
                     f"rseqhockey/{TEAM_ID}/last_match/attributes", "mdi:scoreboard"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/last_match/state", last_state)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/last_match/attributes", {
        "match": last_match,
        "tous_les_matchs_joues": joues,
    })

    # Joueur suivi
    if TRACKED_PLAYER:
        ps = re.sub(r'[^a-zA-Z0-9]', '_', TRACKED_PLAYER)
        pts = player_info_sr.get("PTS", "0") if player_info_sr else "0"
        publish_config(
            f"homeassistant/sensor/rseqhockey_player_{ps}/config",
            build_config(f"RSEQ Joueur {TRACKED_PLAYER.capitalize()}", f"rseqhockey_player_{ps}",
                         f"rseqhockey/player/{ps}/state",
                         f"rseqhockey/player/{ps}/attributes", "mdi:account-star"))
        publish_to_mqtt(f"rseqhockey/player/{ps}/state", f"SR: {pts} pts")
        publish_to_mqtt(f"rseqhockey/player/{ps}/attributes", {"stats_saison_reguliere": player_info_sr})

    # Stats saison
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_stats_saison_reguliere/config",
        build_config(f"RSEQ Stats Saison", f"rseqhockey_{TEAM_ID}_stats_saison_reguliere",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", "mdi:hockey-sticks"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state", f"{len(player_stats_sr)} joueurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", {"joueurs": player_stats_sr})

    print("\n[FIN] Cycle terminé.")


if __name__ == "__main__":
    while True:
        asyncio.run(scrape_and_publish())
        print(f"\n[ATTENTE] {INTERVAL / 3600}h...")
        time.sleep(INTERVAL)
