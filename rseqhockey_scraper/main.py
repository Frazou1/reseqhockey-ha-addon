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

# --- Dates de la saison 2026-27 (extraites du site) ---
SEASON_START = config.get("season_start", "2026-09-19")
SEASON_END = config.get("season_end", "2027-04-01")

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


# ---------- PARSING ROSTER ----------

def parse_roster_structured(html: str):
    try:
        with open("/share/roster_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    gardiens, joueurs, personnel = [], [], []

    for t_idx, table in enumerate(soup.find_all("table")):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not headers:
            continue
        header_set = set(h.lower() for h in headers)

        if "gardiens" in header_set:
            for tr in table.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 2:
                    continue
                nom, num = split_name_and_number(tds[0])
                if nom:
                    gardiens.append({"nom": nom, "numero": num, "stats": dict(zip(headers, tds))})
        elif "joueurs" in header_set:
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


# ---------- PARSING SCHEDULE ----------

_RE_DATE = re.compile(
    r'((?:LUN|MAR|MER|JEU|VEN|SAM|DIM)\.?\s+\d{1,2}\s+'
    r'(?:JANV|FÉVR|FÉV|MARS|AVR|MAI|JUIN|JUIL|AOÛT|SEPT|OCT|NOV|DÉC)\.?'
    r'(?:\s+\d{2,4})?)',
    re.I
)
_RE_HEURE = re.compile(r'(\d{1,2})\s*[Hh]\s*(\d{2})')
_RE_ARENA = re.compile(
    r'((?:Aréna|Arena|Centre|Complexe|Pavillon|Glace)[^|\n•]{0,120}?)'
    r'(?=\s*(?:SAISON|M\d|D\d|RELÈVE|MIXTE|\d{1,2}\s*[Hh]|[A-Z]{4,}|$))',
    re.I
)
_RE_EQUIPE = re.compile(r'\b([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ\-\'\. ]{3,60})\b')

_BLACKLIST_EQUIPES = {
    "SAISON RÉGULIÈRE", "SAISON REGULIERE", "SAISON", "RÉGULIÈRE",
    "M13", "M15", "M18", "D1", "D2", "D3",
    "RELÈVE", "RELEVE", "MIXTE", "HOCKEY", "RSEQ", "LIGUE",
    "VEN", "LUN", "MAR", "MER", "JEU", "SAM", "DIM",
    "JANV", "FÉVR", "FÉV", "MARS", "AVR", "MAI", "JUIN", "JUIL",
    "AOÛT", "SEPT", "OCT", "NOV", "DÉC",
    "ARÉNA", "ARENA", "CENTRE", "COMPLEXE", "PAVILLON", "GLACE",
    "PRÉCÉDENT", "SUIVANT", "FILTRES", "IMPRIMER", "AJOUTER",
    "AUCUNE PARTIE", "AUCUNE", "PARTIE", "TROUVÉE",
    "PERSONNALISÉ", "AUJOURD'HUI", "HIER", "PROCHAINS JOURS",
    "DERNIERS JOURS",
}


def _extraire_equipes(text):
    equipes = []
    for em in _RE_EQUIPE.finditer(text):
        cand = em.group(1).strip()
        if len(cand) < 4:
            continue
        if cand in _BLACKLIST_EQUIPES:
            continue
        if any(cand.startswith(bl + " ") or cand == bl for bl in _BLACKLIST_EQUIPES):
            continue
        if cand not in equipes:
            equipes.append(cand)
    return sorted(equipes, key=len, reverse=True)[:2]


def _extraire_arena(text):
    m = _RE_ARENA.search(text)
    if not m:
        return ""
    arena = re.sub(r'\s+', ' ', m.group(1)).strip().rstrip(',-')
    arena = re.sub(r'\s*[|•]\s*', ' - ', arena)
    return arena


def parse_schedule_structured(html: str):
    try:
        with open("/share/schedule_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")
    matchs = []

    page_text = soup.get_text(" ", strip=True)
    if "Aucune partie n'a été trouvée" in page_text:
        print("[SCHEDULE] ⚠ Page 'Aucune partie trouvée'.")

    blocks = []
    for a in soup.find_all("a", href=re.compile(r'/fr/teams/\d+')):
        parent = a
        for _ in range(6):
            if parent.parent:
                parent = parent.parent
        if parent not in blocks:
            blocks.append(parent)

    for sel in [
        "div.card.rounded.mt-3.p-3", "div.card.rounded.mt-3", "div.card.rounded",
        "div[class*='game-card']", "div[class*='match-card']",
        "div[class*='schedule']", "li[class*='game']", "article[class*='game']",
    ]:
        for el in soup.select(sel):
            if el not in blocks:
                blocks.append(el)

    if not blocks:
        for tag in soup.find_all(string=_RE_ARENA):
            p = tag.find_parent()
            if p:
                for _ in range(6):
                    if p.parent:
                        p = p.parent
                if p not in blocks:
                    blocks.append(p)

    seen = set()
    unique_blocks = []
    for b in blocks:
        if id(b) not in seen:
            seen.add(id(b))
            unique_blocks.append(b)

    print(f"[SCHEDULE] {len(unique_blocks)} bloc(s) candidat(s).")

    for block in unique_blocks:
        text = block.get_text(" ", strip=True)
        if not text or len(text) > 4000:
            continue
        date_m = _RE_DATE.search(text)
        heure_m = _RE_HEURE.search(text)
        if not date_m or not heure_m:
            continue
        matchs.append({
            "date": date_m.group(1).strip(),
            "heure": f"{heure_m.group(1)}h{heure_m.group(2)}",
            "arene": _extraire_arena(text),
            "equipes": _extraire_equipes(text),
        })

    unique_matchs = []
    seen_keys = set()
    for m in matchs:
        key = (m["date"], m["heure"], m["arene"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique_matchs.append(m)

    dates_uniques = list(dict.fromkeys([m["date"] for m in unique_matchs]))
    print(f"[PARSING SCHEDULE] {len(unique_matchs)} match(s) extrait(s).")
    for m in unique_matchs[:5]:
        print(f"   → {m['date']} {m['heure']} | {m['arene']} | {m['equipes']}")

    return {"matchs": unique_matchs, "dates_trouvees": dates_uniques, "prochains_matchs": unique_matchs}


# ---------- PARSING STATS & STANDINGS ----------

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


def parse_standings(html: str, category_uuid: str = None):
    try:
        with open("/share/standings_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass
    soup = BeautifulSoup(html, "html.parser")
    all_rows, best_table_rows = [], []
    for table in soup.find_all("table"):
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
            for r in rows:
                if MY_TEAM_NAME in r.get("Équipe", "").upper():
                    best_table_rows = rows
                    break
    return all_rows, best_table_rows


# ---------- MAIN ----------

async def scrape_and_publish():
    print(f"\n{'='*50}")
    print(f"[START] RseqHockey v2.5.0 - {MY_TEAM_NAME}")
    print(f"{'='*50}")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    # URL horaire avec les vraies dates de saison
    schedule_url = (
        f"{team_url}?tab=schedule"
        f"&dateFrom={SEASON_START}&dateTo={SEASON_END}"
    )

    urls = {
        "roster": f"{team_url}?tab=roster",
        "schedule": schedule_url,
        "standings": f"{team_url}?tab=standings",
        "stats_saison_reguliere": f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
    }

    print(f"[CONFIG] Saison: {SEASON_START} → {SEASON_END}")

    scraped_html = {}
    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key, url in urls.items():
            html = await fetch_with_flaresolverr(session, url, key)
            scraped_html[key] = html
            if html is None:
                print(f"[ABORT] Impossible de récupérer '{key}'.")
                return

    print("\n[PARSING]...")
    roster_data = parse_roster_structured(scraped_html["roster"])
    schedule_data = parse_schedule_structured(scraped_html["schedule"])
    player_stats_sr = parse_table_generic(scraped_html["stats_saison_reguliere"], "stats_saison_reguliere")
    standings_all, standings_my_team = parse_standings(scraped_html["standings"])

    my_team_info = next((t for t in standings_all if MY_TEAM_NAME in t.get("Équipe", "").upper()), None)
    player_info_sr = next((p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()), None) if TRACKED_PLAYER else None

    print("\n[MQTT] Publication...")

    def build_config(name, oid, st, at, icon):
        return {"name": name, "object_id": oid, "unique_id": oid,
                "state_topic": st, "json_attributes_topic": at,
                "icon": icon, "device": DEVICE_INFO}

    def publish_config(topic, cfg):
        print(f"\n[MQTT DISCOVERY] {topic}")
        publish_to_mqtt(topic, cfg)

    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])

    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Alignement {TEAM_ID}", f"rseqhockey_{TEAM_ID}_roster",
                     f"rseqhockey/{TEAM_ID}/roster/state",
                     f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state",
                    f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    if schedule_data["prochains_matchs"]:
        nm = schedule_data["prochains_matchs"][0]
        state = f"{nm['date']} {nm['heure']} - {nm['arene'] or 'Aréna inconnu'}"
    else:
        state = "Aucun match prévu"

    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_schedule/config",
        build_config(f"RSEQ Horaire {TEAM_ID}", f"rseqhockey_{TEAM_ID}_schedule",
                     f"rseqhockey/{TEAM_ID}/schedule/state",
                     f"rseqhockey/{TEAM_ID}/schedule/attributes", "mdi:calendar-clock"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/state", state)
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/schedule/attributes", schedule_data)

    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config",
        build_config(f"RSEQ Classement {MY_TEAM_NAME.capitalize()}", f"rseqhockey_{TEAM_ID}_standings",
                     f"rseqhockey/{TEAM_ID}/standings/state",
                     f"rseqhockey/{TEAM_ID}/standings/attributes", "mdi:trophy"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state",
                    f"{my_team_info.get('#', 'N/A')}e rang" if my_team_info else "Saison en cours")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes",
                    {"mon_equipe": my_team_info, "classement_complet": standings_all})

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

    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_stats_saison_reguliere/config",
        build_config(f"RSEQ Stats Saison {TEAM_ID}", f"rseqhockey_{TEAM_ID}_stats_saison_reguliere",
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
