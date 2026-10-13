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

# --- API Spordle ---
SPORDLE_API = "https://pub-api.play.spordle.com/api"
SPORDLE_API_KEY = "f08ed9064e3cdc382e6abb305ff543d0150fb52f"
OFFICE_ID = 6241
CATEGORY_ID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SEASON_ID = "2026-27"
SEASON_START = config.get("season_start", "2026-09-19")
SEASON_END = config.get("season_end", "2027-04-01")
TIMEZONE = config.get("timezone", "America/Toronto")

LEAGUE_UUID = CATEGORY_ID
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

API_HEADERS = {
    "x-api-key": SPORDLE_API_KEY,
    "Origin": "https://scolaire.rseqhockey.com",
    "Referer": "https://scolaire.rseqhockey.com/",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}


# ---------- API SPORDLE ----------

async def api_get_games(session):
    filt = {
        "order": ["startTime ASC", "number ASC"],
        "where": {
            "and": [
                {"date": {"between": [SEASON_START, SEASON_END]}},
                {"or": [{"awayTeamId": int(TEAM_ID)}, {"homeTeamId": int(TEAM_ID)}]},
                {"officeId": OFFICE_ID},
            ]
        },
        "include": ["teamStats", "surface", "office", "category",
                    "awayTeam", "homeTeam", "externalProviders"],
    }
    filt_str = json.dumps(filt, separators=(',', ':'))
    url = f"{SPORDLE_API}/sp/games"
    params = {"filter": filt_str}

    print(f"\n[API] GET {url}")
    try:
        async with session.get(url, params=params, headers=API_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            print(f"[API] HTTP {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                print(f"[API OK] {len(data) if isinstance(data, list) else '?'} game(s)")
                return data
            else:
                txt = await resp.text()
                print(f"[API ERROR] {resp.status}: {txt[:300]}")
    except Exception as e:
        print(f"[API ERROR] {e}")
    return None


async def api_get_standings(session):
    """Classement via l'API."""
    url = f"{SPORDLE_API}/sp/standings"
    filt = {
        "where": {"and": [
            {"categoryId": CATEGORY_ID},
            {"officeId": OFFICE_ID},
        ]},
        "include": ["team"],
    }
    params = {"filter": json.dumps(filt, separators=(',', ':'))}
    try:
        async with session.get(url, params=params, headers=API_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"[API STANDINGS OK] {len(json.dumps(data))} octets")
                return data
            else:
                print(f"[API STANDINGS] HTTP {resp.status}")
    except Exception as e:
        print(f"[API STANDINGS ERROR] {e}")
    return None


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


# ---------- UTILS ----------

_JOURS_FR = ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"]
_MOIS_FR = ["JANV", "FÉVR", "MARS", "AVR", "MAI", "JUIN",
            "JUIL", "AOÛT", "SEPT", "OCT", "NOV", "DÉC"]


def iso_to_local_parts(iso_str, tz_name):
    """Convertit un ISO UTC en (date_fr, heure_fr, iso_local)."""
    if not iso_str:
        return "", "", ""
    try:
        # Parse ISO UTC
        dt_utc = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # Convertir en local
        tz = ZoneInfo(tz_name)
        dt_local = dt_utc.astimezone(tz)
        date_fr = f"{_JOURS_FR[dt_local.weekday()]}. {dt_local.day} {_MOIS_FR[dt_local.month - 1]}."
        heure_fr = dt_local.strftime("%Hh%M")
        return date_fr, heure_fr, dt_local.isoformat()
    except Exception as e:
        print(f"[TZ ERROR] {iso_str}: {e}")
        return iso_str, "", iso_str


def parse_games(api_data):
    """Parse la liste des matchs depuis l'API."""
    if not api_data or not isinstance(api_data, list):
        print("[PARSE GAMES] Pas de données.")
        return []

    matchs = []
    for g in api_data:
        if not isinstance(g, dict):
            continue

        # Date + heure (conversion UTC → local)
        iso = g.get("startTime", "")
        tz_name = g.get("timezone", TIMEZONE)
        date_fr, heure_fr, iso_local = iso_to_local_parts(iso, tz_name)

        # Aréna
        surface = g.get("surface") or {}
        venue = surface.get("venue") or {}
        arena_parts = []
        if venue.get("name"):
            arena_parts.append(venue["name"])
        if surface.get("name") and surface["name"].lower() not in (venue.get("name", "").lower(),):
            arena_parts.append(f"({surface['name']})")
        if venue.get("city"):
            arena_parts.append(f"- {venue['city']}")
        arena = " ".join(arena_parts) if arena_parts else ""

        # Équipes
        home = (g.get("homeTeam") or {}).get("name", "")
        away = (g.get("awayTeam") or {}).get("name", "")

        # Score (dict {teamId: score})
        score_dict = g.get("score") or {}
        home_score = score_dict.get(str(g.get("homeTeamId"))) or score_dict.get(g.get("homeTeamId"))
        away_score = score_dict.get(str(g.get("awayTeamId"))) or score_dict.get(g.get("awayTeamId"))
        score = ""
        if home_score is not None and away_score is not None:
            score = f"{away_score}-{home_score}"

        # Résultat pour notre équipe
        our_score = None
        opp_score = None
        result = ""
        if str(TEAM_ID) == str(g.get("homeTeamId")):
            our_score = home_score
            opp_score = away_score
        elif str(TEAM_ID) == str(g.get("awayTeamId")):
            our_score = away_score
            opp_score = home_score

        if our_score is not None and opp_score is not None:
            if our_score > opp_score:
                result = "V"
            elif our_score < opp_score:
                result = "D"
            else:
                result = "N"

        # Est-ce joué ?
        played = g.get("actualEndTime") is not None or bool(score)

        match = {
            "numero": g.get("number", ""),
            "date": date_fr,
            "date_iso": g.get("date", ""),
            "heure": heure_fr,
            "iso_local": iso_local,
            "arene": arena,
            "equipe_domicile": home,
            "equipe_visiteur": away,
            "equipes": [e for e in [away, home] if e],
            "score": score,
            "notre_score": our_score,
            "score_adversaire": opp_score,
            "resultat": result,
            "joue": played,
            "is_live": g.get("isLive", False),
            "statut": g.get("status", ""),
        }
        matchs.append(match)

    # Trier par date ISO
    matchs.sort(key=lambda m: m.get("date_iso", ""))
    print(f"[PARSE GAMES] {len(matchs)} match(s) au total.")
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
    print(f"[START] RseqHockey v5.0.0 (API) - {MY_TEAM_NAME}")
    print(f"{'='*50}")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        print("\n[ÉTAPE 1] API /sp/games")
        games_data = await api_get_games(session)

        print("\n[ÉTAPE 2] API /sp/standings")
        standings_data = await api_get_standings(session)

        print("\n[ÉTAPE 3] FlareSolverr pour roster + stats")
        roster_html = await fetch_with_flaresolverr(session, f"{team_url}?tab=roster", "roster")
        stats_html = await fetch_with_flaresolverr(
            session,
            f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
            "stats_saison_reguliere"
        )

    # --- PARSING ---
    print("\n[PARSING]...")
    matchs = parse_games(games_data) if games_data else []

    roster_data = parse_roster_structured(roster_html) if roster_html else {"gardiens": [], "joueurs": [], "personnel": []}
    player_stats_sr = parse_table_generic(stats_html, "stats_saison_reguliere") if stats_html else []

    # Séparer joués / à venir
    joues = [m for m in matchs if m["joue"]]
    a_venir = [m for m in matchs if not m["joue"]]

    last_match = joues[-1] if joues else None
    next_match = a_venir[0] if a_venir else None

    print(f"[STATS] {len(joues)} joué(s), {len(a_venir)} à venir.")
    if last_match:
        print(f"   Dernier: {last_match['date']} {last_match['equipe_visiteur']} {last_match['score']} {last_match['equipe_domicile']}")
    if next_match:
        print(f"   Prochain: {next_match['date']} {next_match['heure']} @ {next_match['arene']}")

    # Classement : chercher notre équipe
    standings_list = []
    if standings_data:
        raw_std = standings_data.get("data") if isinstance(standings_data, dict) else standings_data
        if isinstance(raw_std, list):
            standings_list = raw_std

    # Player suivi
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

    # --- ROSTER ---
    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Alignement", f"rseqhockey_{TEAM_ID}_roster",
                     f"rseqhockey/{TEAM_ID}/roster/state",
                     f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state",
                    f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

    # --- PROCHAIN MATCH ---
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
        "nb_matchs_a_venir": len(a_venir),
    })

    # --- DERNIER MATCH (avec score et résultat) ---
    if last_match:
        # Format : "VEN. 02 OCT. : ÉCOLE SEC. DE LA SEIGNEURIE 1-0 ÉCOLE SEC. POINTE-LÉVY (V)"
        if last_match.get("equipe_visiteur") and last_match.get("equipe_domicile"):
            last_state = (
                f"{last_match['date']} : "
                f"{last_match['equipe_visiteur']} {last_match['score']} "
                f"{last_match['equipe_domicile']}"
            )
            if last_match.get("resultat"):
                last_state += f" ({last_match['resultat']})"
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

    # --- CLASSEMENT ---
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config",
        build_config(f"RSEQ Classement", f"rseqhockey_{TEAM_ID}_standings",
                     f"rseqhockey/{TEAM_ID}/standings/state",
                     f"rseqhockey/{TEAM_ID}/standings/attributes", "mdi:trophy"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state",
                    f"{len(standings_list)} équipes" if standings_list else "Saison en cours")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes",
                    {"classement": standings_list})

    # --- JOUEUR SUIVI ---
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

    # --- STATS SAISON ---
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
