import json
import time
import re
import os
import asyncio
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
from playwright.async_api import async_playwright

with open("/data/options.json", "r") as f:
    config = json.load(f)

TEAM_ID = config.get("team_id", "000001")
MY_TEAM_NAME = config.get("team_name", "MON ECOLE").upper()
TRACKED_PLAYER = config.get("tracked_player", "").strip().lower()

MQTT_HOST = config.get("mqtt_host", "core-mosquitto")
MQTT_PORT = config.get("mqtt_port", 1883)
MQTT_USER = config.get("mqtt_user", "")
MQTT_PASS = config.get("mqtt_password", "")
INTERVAL = config.get("update_interval_hours", 6) * 3600

FLARESOLVERR_URL = config.get("flaresolverr_url", "http://192.168.1.65:8191/v1")
FLARESOLVERR_TIMEOUT = config.get("flaresolverr_timeout", 90)

LEAGUE_UUID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SCHEDULE_SAISON_REGULIERE = "198862"
BASE_WEB = "https://scolaire.rseqhockey.com/fr"

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


# ============================================================
# ÉTAPE 1 : FlareSolverr récupère les cookies Cloudflare
# ============================================================

async def get_cloudflare_cookies(session):
    print("\n" + "="*60)
    print("[ÉTAPE 1] FlareSolverr : obtention des cookies Cloudflare")
    print("="*60)
    payload = {
        "cmd": "request.get",
        "url": f"{BASE_WEB}/teams/{TEAM_ID}?tab=schedule",
        "maxTimeout": FLARESOLVERR_TIMEOUT * 1000,
    }
    try:
        async with session.post(FLARESOLVERR_URL, json=payload) as resp:
            data = await resp.json()
    except Exception as e:
        print(f"[FLARESOLVERR ERROR] {e}")
        return None, None

    status = data.get("status")
    if status != "ok":
        print(f"[FLARESOLVERR ERROR] status={status} | {data.get('message')}")
        return None, None

    solution = data.get("solution", {})
    cookies = solution.get("cookies", [])
    html = solution.get("response", "")
    user_agent = solution.get("userAgent", "")

    print(f"[FLARESOLVERR OK] {len(cookies)} cookie(s) récupéré(s), HTML: {len(html)} octets")
    for c in cookies:
        print(f"   → {c.get('name')} (domain={c.get('domain')})")
    if user_agent:
        print(f"   User-Agent: {user_agent[:80]}...")

    return cookies, user_agent


# ============================================================
# ÉTAPE 2 : Playwright avec cookies pré-injectés
# ============================================================

async def fetch_schedule_with_playwright(team_url, cookies, user_agent):
    print("\n" + "="*60)
    print("[ÉTAPE 2] Playwright : navigation + clics")
    print("="*60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=ua,
            locale="fr-CA",
            timezone_id="America/Toronto",
        )

        # --- Injection des cookies Cloudflare ---
        if cookies:
            pw_cookies = []
            for c in cookies:
                cookie = {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "path": c.get("path", "/"),
                }
                if c.get("expires") and c.get("expires") > 0:
                    cookie["expires"] = c["expires"]
                if c.get("secure") is not None:
                    cookie["secure"] = c["secure"]
                if c.get("httpOnly") is not None:
                    cookie["httpOnly"] = c["httpOnly"]
                pw_cookies.append(cookie)
            await context.add_cookies(pw_cookies)
            print(f"[PLAYWRIGHT] {len(pw_cookies)} cookie(s) injecté(s).")

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['fr-CA', 'fr', 'en-US', 'en']});
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        print(f"[PLAYWRIGHT] Navigation vers {team_url}")
        try:
            await page.goto(team_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[PLAYWRIGHT ERROR] goto: {e}")
            await browser.close()
            return None

        # Attente Cloudflare
        for i in range(30):
            title = (await page.title()).lower()
            if "un instant" not in title and "just a moment" not in title:
                print(f"[PLAYWRIGHT] Cloudflare OK après {i+1}s (titre: '{await page.title()}')")
                break
            if i % 5 == 0:
                print(f"[PLAYWRIGHT] Challenge en cours ({i+1}s)...")
            await page.wait_for_timeout(1000)
        else:
            print("[PLAYWRIGHT ERROR] Cloudflare non franchi.")
            await browser.close()
            return None

        await page.wait_for_timeout(3000)

        # Attente fin skeletons
        print("[PLAYWRIGHT] Attente fin du chargement (skeletons)...")
        for i in range(20):
            sk = await page.locator(".spdl-skeleton").count()
            if sk == 0:
                print(f"[PLAYWRIGHT] Skeletons disparus après {i+1}s")
                break
            await page.wait_for_timeout(1000)

        # --- Clic 1 : dropdown ---
        print("\n[PLAYWRIGHT] Clic 1/3 : ouvrir le dropdown de filtre")
        try:
            btn = page.locator(
                "button:has-text('prochains jours'), "
                "button:has-text('prochain jour'), "
                "button:has-text('jours')"
            ).first
            await btn.wait_for(state="visible", timeout=15000)
            await btn.click(timeout=5000)
            print("[PLAYWRIGHT] ✓ Dropdown ouvert")
            await page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[PLAYWRIGHT] ✗ Échec clic 1 : {e}")
            try:
                with open("/share/schedule_debug_fail.html", "w", encoding="utf-8") as f:
                    f.write(await page.content())
            except Exception:
                pass

        # --- Clic 2 : Saison 2026-27 ---
        print("\n[PLAYWRIGHT] Clic 2/3 : 'Saison 2026-27'")
        try:
            li = page.locator("li:has-text('Saison')").first
            await li.wait_for(state="visible", timeout=10000)
            await li.click(timeout=5000)
            print("[PLAYWRIGHT] ✓ Saison cliquée")
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[PLAYWRIGHT] ✗ Échec clic 2 : {e}")

        # --- Clic 3 : Appliquer ---
        print("\n[PLAYWRIGHT] Clic 3/3 : 'Appliquer'")
        try:
            btn = page.locator("button:has-text('Appliquer')").first
            await btn.wait_for(state="visible", timeout=10000)
            await btn.click(timeout=5000)
            print("[PLAYWRIGHT] ✓ Appliquer cliqué")
        except Exception as e:
            print(f"[PLAYWRIGHT] ✗ Échec clic 3 : {e}")

        # --- Attente chargement matchs ---
        print("\n[PLAYWRIGHT] Attente chargement des matchs...")
        for i in range(30):
            sk = await page.locator(".spdl-skeleton").count()
            arts = await page.locator("article[itemtype*='SportsEvent']").count()
            if sk == 0 and arts > 0:
                print(f"[PLAYWRIGHT] ✓ {arts} match(s) chargés après {i+1}s")
                break
            if i % 5 == 0:
                print(f"[PLAYWRIGHT] ...en cours ({i+1}s, {arts} articles)")
            await page.wait_for_timeout(1000)

        await page.wait_for_timeout(2000)
        html = await page.content()
        print(f"\n[PLAYWRIGHT OK] HTML final: {len(html)} octets")

        try:
            with open("/share/schedule_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("[PLAYWRIGHT] HTML sauvegardé dans /share/schedule_debug.html")
        except Exception:
            pass

        count = html.count('data-date-section')
        print(f"[PLAYWRIGHT] {count} section(s) data-date-section détectée(s).")

        await browser.close()
        return html


# ============================================================
# FLARESOLVERR (pour roster + stats)
# ============================================================

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


# ============================================================
# MQTT
# ============================================================

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


# ============================================================
# LOGOS
# ============================================================

def _slugify(name: str) -> str:
    """Convertit un nom d'équipe en slug ASCII sûr pour nom de fichier."""
    # Remplacement des accents courants
    replacements = {
        'à': 'a', 'â': 'a', 'ä': 'a', 'á': 'a', 'ã': 'a', 'å': 'a',
        'ç': 'c', 'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
        'î': 'i', 'ï': 'i', 'í': 'i', 'ì': 'i',
        'ô': 'o', 'ö': 'o', 'ó': 'o', 'ò': 'o', 'õ': 'o',
        'ù': 'u', 'û': 'u', 'ü': 'u', 'ú': 'u',
        'ÿ': 'y', 'ñ': 'n', "'": '', '’': '',
    }
    s = name.lower()
    for k, v in replacements.items():
        s = s.replace(k, v)
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


def download_logo(url: str, team_name: str) -> str:
    """Télécharge un logo dans /share/rseqhockey_logos/ et retourne le chemin HTTP local."""
    if not url:
        return ""
    try:
        slug = _slugify(team_name)
        ext = os.path.splitext(url.split("?")[0])[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            ext = ".png"
        filename = f"{slug}{ext}"
        filepath = os.path.join(LOGOS_DIR, filename)

        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            with open(filepath, "wb") as f:
                f.write(data)
            print(f"[LOGO] Téléchargé: {team_name} → {filename} ({len(data)} octets)")
        else:
            print(f"[LOGO] Déjà présent: {filename}")

        return f"/local/rseqhockey_logos/{filename}"
    except Exception as e:
        print(f"[LOGO ERROR] {team_name}: {e}")
        return ""


# ============================================================
# PARSING SCHEDULE
# ============================================================

_JOURS_FR = ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"]
_MOIS_FR = ["JANV", "FÉVR", "MARS", "AVR", "MAI", "JUIN",
            "JUIL", "AOÛT", "SEPT", "OCT", "NOV", "DÉC"]

_MOIS_MAP = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_JOURS_MAP = {
    "lundi": "LUN", "mardi": "MAR", "mercredi": "MER",
    "jeudi": "JEU", "vendredi": "VEN", "samedi": "SAM", "dimanche": "DIM",
}
_MOIS_ABBR = {
    1: "JANV", 2: "FÉVR", 3: "MARS", 4: "AVR", 5: "MAI", 6: "JUIN",
    7: "JUIL", 8: "AOÛT", 9: "SEPT", 10: "OCT", 11: "NOV", 12: "DÉC",
}


def _parse_date_header(h4_text: str):
    """Convertit 'vendredi 2 octobre 2026' en ('VEN. 2 OCT.', '2026-10-02')."""
    m = re.match(r'(\w+)\s+(\d{1,2})\s+(\w+)\s+(\d{4})', h4_text.strip(), re.I)
    if not m:
        return h4_text, ""
    jour, num, mois, annee = m.group(1).lower(), int(m.group(2)), m.group(3).lower(), int(m.group(4))
    j_abbr = _JOURS_MAP.get(jour, jour[:3].upper())
    m_num = _MOIS_MAP.get(mois, 1)
    date_fr = f"{j_abbr}. {num} {_MOIS_ABBR[m_num]}."
    date_iso = f"{annee:04d}-{m_num:02d}-{num:02d}"
    return date_fr, date_iso


def _iso_to_date_fr(iso):
    """Convertit un ISO UTC en date FR + heure locale."""
    try:
        iso_clean = iso.strip().replace(" ", "")
        dt = datetime.fromisoformat(iso_clean.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto"))
        return f"{_JOURS_FR[dt.weekday()]}. {dt.day} {_MOIS_FR[dt.month-1]}.", dt.strftime("%Hh%M")
    except Exception as e:
        print(f"[TZ WARN] {iso}: {e}")
        return "", ""


def parse_schedule_cards(html: str):
    soup = BeautifulSoup(html, "html.parser")
    matchs = []

    for section_li in soup.find_all("li", attrs={"data-date-section": True}):
        h4 = section_li.find("h4")
        section_date_fr = ""
        section_date_iso = ""
        if h4:
            section_date_fr, section_date_iso = _parse_date_header(h4.get_text(" ", strip=True))
            print(f"[SCHEDULE] Section: '{h4.get_text(strip=True)}' → {section_date_fr} ({section_date_iso})")

        articles = section_li.find_all("article", attrs={"itemtype": re.compile("SportsEvent", re.I)})
        for art in articles:
            # Aréna
            arena = ""
            addr = art.find("address")
            if addr:
                arena = addr.get_text(" ", strip=True)

            # Numéro
            num_match = ""
            sched_type = art.find("span", class_=re.compile("scheduleType", re.I))
            if sched_type:
                txt = sched_type.get_text(" ", strip=True)
                m = re.search(r'P-?\d+', txt)
                if m:
                    num_match = m.group(0)

            # Date/heure
            iso = ""
            date_fr = section_date_fr
            heure_fr = ""
            t = art.find("time")
            if t:
                iso = (t.get("datetime") or "").strip()
                if iso:
                    d_fr, h_fr = _iso_to_date_fr(iso)
                    if d_fr:
                        date_fr = d_fr
                        heure_fr = h_fr

            # Score + statut
            score = ""
            status = "À VENIR"
            final_el = art.find(string=re.compile(
                r'^\s*(FINAL|EN COURS|PROLONGATION|FUSILLADE)\s*$', re.I))
            if final_el:
                status = final_el.strip().upper()
                score_divs = art.find_all("div", class_=re.compile("font-brand font-size-lg", re.I))
                digits = [d.get_text(strip=True) for d in score_divs if d.get_text(strip=True).isdigit()]
                if len(digits) >= 2:
                    score = f"{digits[0]}-{digits[1]}"

            # Équipes + logos
            equipes = []
            equipes_details = []
            teams = art.find_all("article", attrs={"itemtype": re.compile("SportsTeam", re.I)})
            for t_art in teams:
                team_name = ""
                name_el = t_art.find(attrs={"itemprop": "name"})
                if name_el:
                    a = name_el.find("a")
                    if a:
                        team_name = a.get_text(strip=True)

                logo_url = ""
                img = t_art.find("img")
                if img:
                    logo_url = img.get("src", "")

                logo_local = ""
                if team_name and logo_url:
                    logo_local = download_logo(logo_url, team_name)

                if team_name:
                    equipes.append(team_name)
                    equipes_details.append({
                        "nom": team_name,
                        "logo_remote": logo_url,
                        "logo_local": logo_local,
                    })

            joue = bool(score)
            match = {
                "numero": num_match,
                "date": date_fr,
                "date_iso": section_date_iso,
                "heure": heure_fr,
                "iso": iso,
                "arene": arena,
                "equipes": equipes,
                "equipes_details": equipes_details,
                "score": score,
                "statut": status,
                "joue": joue,
            }
            matchs.append(match)
            print(f"   {date_fr} {heure_fr} | {arena[:55]} | {equipes} | {score or '-'} | {status}")
            for e in equipes_details:
                print(f"      └─ {e['nom']} → logo: {e['logo_local'] or '(échec)'}")

    return matchs


# ============================================================
# PARSING ROSTER
# ============================================================

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


# ============================================================
# MAIN
# ============================================================

async def scrape_and_publish():
    print(f"\n{'='*60}")
    print(f"[START] RseqHockey v2.9.0 - {MY_TEAM_NAME}")
    print(f"{'='*60}")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        # --- 1. FlareSolverr : cookies ---
        cookies, user_agent = await get_cloudflare_cookies(session)
        if not cookies:
            print("[ABORT] Impossible d'obtenir les cookies Cloudflare.")
            return

        # --- 2. Playwright : schedule ---
        schedule_html = await fetch_schedule_with_playwright(
            f"{team_url}?tab=schedule", cookies, user_agent
        )

        # --- 3. FlareSolverr : roster + stats ---
        roster_html = await fetch_with_flaresolverr(session, f"{team_url}?tab=roster", "roster")
        stats_html = await fetch_with_flaresolverr(
            session,
            f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
            "stats_saison_reguliere"
        )

    print("\n[PARSING]...")
    matchs = parse_schedule_cards(schedule_html) if schedule_html else []
    roster_data = parse_roster_structured(roster_html) if roster_html else {"gardiens": [], "joueurs": [], "personnel": []}
    player_stats_sr = parse_table_generic(stats_html, "stats_saison_reguliere") if stats_html else []

    joues = [m for m in matchs if m["joue"]]
    a_venir = [m for m in matchs if not m["joue"]]
    joues.sort(key=lambda m: m.get("date_iso", ""))
    a_venir.sort(key=lambda m: m.get("date_iso", ""))

    last_match = joues[-1] if joues else None
    next_match = a_venir[0] if a_venir else None

    print(f"\n[STATS] {len(joues)} joué(s), {len(a_venir)} à venir.")
    if last_match:
        print(f"   Dernier: {last_match['date']} {last_match['equipes']} {last_match['score']}")
    if next_match:
        print(f"   Prochain: {next_match['date']} {next_match['heure']} @ {next_match['arene']}")

    player_info_sr = next(
        (p for p in player_stats_sr if TRACKED_PLAYER in p.get("Nom", "").lower()),
        None
    ) if TRACKED_PLAYER else None

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
