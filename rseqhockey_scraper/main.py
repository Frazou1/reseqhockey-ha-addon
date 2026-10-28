import json
import time
import re
import os
import asyncio
import urllib.request
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
from playwright.async_api import async_playwright
import rythme

# Matchs du dernier cycle, pour le rythme adaptatif / last cycle's games, for the adaptive rhythm
DERNIERS_MATCHS = []

with open("/data/options.json", "r") as f:
    config = json.load(f)

TEAM_ID = config.get("team_id", "")
MY_TEAM_NAME = config.get("team_name", "").upper()
TRACKED_PLAYER = config.get("tracked_player", "").strip().lower()

MQTT_HOST = config.get("mqtt_host", "core-mosquitto")
MQTT_PORT = config.get("mqtt_port", 1883)
MQTT_USER = config.get("mqtt_user", "")
MQTT_PASS = config.get("mqtt_password", "")
INTERVAL = config.get("update_interval_hours", 6) * 3600

FLARESOLVERR_URL = config.get("flaresolverr_url", "http://localhost:8191/v1")
FLARESOLVERR_TIMEOUT = config.get("flaresolverr_timeout", 90)

LEAGUE_UUID = "ae5bed83-a302-4ac5-927b-639d2c20a3c9"
SCHEDULE_SAISON_REGULIERE = "198862"
BASE_WEB = "https://scolaire.rseqhockey.com/fr"

# Logos écrits directement dans /config/www : HA ne sert pas /local à travers un lien symbolique vers /share.
# Logos written straight into /config/www: HA won't serve /local through a symlink to /share.
WWW_LOGOS_DIR = "/config/www/rseqhockey_logos"
if os.path.islink(WWW_LOGOS_DIR):
    os.unlink(WWW_LOGOS_DIR)
LOGOS_DIR = WWW_LOGOS_DIR if os.path.isdir("/config") else "/share/rseqhockey_logos"
os.makedirs(LOGOS_DIR, exist_ok=True)

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
        )

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

        print("[PLAYWRIGHT] Attente fin du chargement (skeletons)...")
        for i in range(20):
            sk = await page.locator(".spdl-skeleton").count()
            if sk == 0:
                print(f"[PLAYWRIGHT] Skeletons disparus après {i+1}s")
                break
            await page.wait_for_timeout(1000)

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

        print("\n[PLAYWRIGHT] Clic 2/3 : 'Saison 2026-27'")
        try:
            li = page.locator("li:has-text('Saison')").first
            await li.wait_for(state="visible", timeout=10000)
            await li.click(timeout=5000)
            print("[PLAYWRIGHT] ✓ Saison cliquée")
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[PLAYWRIGHT] ✗ Échec clic 2 : {e}")

        print("\n[PLAYWRIGHT] Clic 3/3 : 'Appliquer'")
        try:
            btn = page.locator("button:has-text('Appliquer')").first
            await btn.wait_for(state="visible", timeout=10000)
            await btn.click(timeout=5000)
            print("[PLAYWRIGHT] ✓ Appliquer cliqué")
        except Exception as e:
            print(f"[PLAYWRIGHT] ✗ Échec clic 3 : {e}")

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
        # La vue « Saison » n'affiche les cartes de match que lorsqu'elles sont a l'ecran : on defile par
        # etapes et on capture la page a chaque etape (les cartes sont fusionnees au parsing).
        # The Season view only renders game cards while on screen: scroll step by step, snapshot each step.
        captures = [await page.content()]
        hauteur_prec, immobile = 0, 0
        for _ in range(40):
            await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(1500)
            captures.append(await page.content())
            pos = await page.evaluate("[window.scrollY + window.innerHeight, document.body.scrollHeight]")
            if pos[0] >= pos[1] - 5:
                immobile = immobile + 1 if pos[1] == hauteur_prec else 0
                if immobile >= 2:
                    break
            hauteur_prec = pos[1]
        html = captures[-1]
        print(f"\n[PLAYWRIGHT OK] {len(captures)} capture(s) de la vue « Saison », dernière: {len(html)} octets")

        try:
            with open("/share/schedule_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        count = html.count('data-date-section')
        print(f"[PLAYWRIGHT] {count} section(s) data-date-section détectée(s).")

        await browser.close()
        return captures


# Historique des matchs joues, conserve d'un cycle a l'autre : une page incomplete ne fait plus
# disparaitre un match. / Played-games history kept across cycles.
HISTO = "/data/matchs_joues.json"


def merge_schedule(captures):
    """Fusionne les matchs de toutes les captures + l'historique, sans doublons (date, equipes).
    Merges games from all snapshots + history, deduplicated on (date, teams)."""
    try:
        matchs = [m for m in json.load(open(HISTO, encoding="utf-8")) if m.get("joue")]
    except Exception:
        matchs = []
    for cap in captures or []:
        for m in parse_schedule_cards(cap):
            cle = (m.get("date_iso"), tuple(m.get("equipes", [])))
            matchs = [x for x in matchs if (x.get("date_iso"), tuple(x.get("equipes", []))) != cle] + [m]
    try:
        json.dump([m for m in matchs if m.get("joue")], open(HISTO, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        print(f"[HISTO] Écriture impossible: {e}")
    return matchs


# ============================================================
# FLARESOLVERR (pour roster + stats) — avec retry
# ============================================================

async def fetch_with_flaresolverr(session, url, key_name, retries=2):
    for attempt in range(retries):
        print(f"\n[FLARESOLVERR] Requête '{key_name}' (essai {attempt+1}/{retries}): {url}")
        payload = {"cmd": "request.get", "url": url, "maxTimeout": FLARESOLVERR_TIMEOUT * 1000}
        try:
            async with session.post(FLARESOLVERR_URL, json=payload) as resp:
                data = await resp.json()
        except Exception as e:
            print(f"[FLARESOLVERR ERROR '{key_name}'] essai {attempt+1}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
            continue

        if data.get("status") == "ok":
            html = data.get("solution", {}).get("response", "")
            if html:
                print(f"[FLARESOLVERR OK '{key_name}'] {len(html)} octets")
                return html
        else:
            print(f"[FLARESOLVERR ERROR '{key_name}'] essai {attempt+1}: {data.get('message')}")

        if attempt < retries - 1:
            print(f"[FLARESOLVERR] Retry dans 5s...")
            await asyncio.sleep(5)

    return None


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
    try:
        iso_clean = iso.strip().replace(" ", "")
        dt_utc = datetime.fromisoformat(iso_clean.replace("Z", "+00:00"))

        try:
            dt_local = dt_utc.astimezone(ZoneInfo("America/Toronto"))
        except Exception:
            year = dt_utc.year
            march = calendar.monthcalendar(year, 3)
            dst_start_day = [w[6] for w in march if w[6] != 0][1]
            dst_start = datetime(year, 3, dst_start_day, 7, 0)
            nov = calendar.monthcalendar(year, 11)
            dst_end_day = [w[6] for w in nov if w[6] != 0][0]
            dst_end = datetime(year, 11, dst_end_day, 6, 0)
            dt_naive = dt_utc.replace(tzinfo=None)
            offset_h = -4 if dst_start <= dt_naive < dst_end else -5
            dt_local = dt_utc + timedelta(hours=offset_h)

        return f"{_JOURS_FR[dt_local.weekday()]}. {dt_local.day} {_MOIS_FR[dt_local.month-1]}.", dt_local.strftime("%Hh%M")
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
            arena = ""
            addr = art.find("address")
            if addr:
                arena = addr.get_text(" ", strip=True)

            num_match = ""
            sched_type = art.find("span", class_=re.compile("scheduleType", re.I))
            if sched_type:
                txt = sched_type.get_text(" ", strip=True)
                m = re.search(r'P-?\d+', txt)
                if m:
                    num_match = m.group(0)

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

    return matchs


# ============================================================
# PARSING ROSTER
# ============================================================

def _extract_name_from_cell(td):
    """
    Extrait le nom depuis le <div title="NOM"> de Spordle.
    Le title contient 'PRÉNOM NOM' propre.
    """
    # Priorité 1 : attribut title d'un div
    for div in td.find_all("div"):
        title = div.get("title", "").strip()
        if title and len(title) > 2 and not title.isdigit():
            return title

    # Priorité 2 : balise <a> avec texte direct
    a = td.find("a")
    if a:
        txt = a.get_text(strip=True)
        if txt and len(txt) > 2 and not txt.isdigit():
            return txt

    # Priorité 3 : span avec class text-truncate (personnel)
    sp = td.find("span", class_=re.compile("text-truncate", re.I))
    if sp:
        txt = sp.get_text(strip=True)
        if txt and len(txt) > 2:
            return txt

    return ""


def _extract_number_from_cell(td_name, td_num):
    """
    Extrait le numéro. Il est dans :
    <span class='ml-2 text-muted'><span>11</span></span>
    """
    # Priorité 1 : span avec class ml-2 text-muted
    for sp in td_name.find_all("span"):
        classes = " ".join(sp.get("class", []))
        if "text-muted" in classes and "ml-2" in classes:
            num_span = sp.find("span")
            if num_span:
                txt = num_span.get_text(strip=True)
                if txt.isdigit():
                    return txt

    # Priorité 2 : chercher le dernier <span> du td qui ne contient qu'un nombre 1-2 chiffres
    all_spans = td_name.find_all("span")
    for sp in reversed(all_spans):
        txt = sp.get_text(strip=True)
        if txt.isdigit() and 1 <= int(txt) <= 99:
            return txt

    return ""


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
            print(f"\n[ROSTER] Table GARDIENS")
            for tr in table.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                nom = _extract_name_from_cell(tds[0])
                num = _extract_number_from_cell(tds[0], tds[1])
                if nom:
                    gardiens.append({
                        "nom": nom,
                        "numero": num,
                        "stats": dict(zip(headers, [td.get_text(strip=True) for td in tds]))
                    })
                    print(f"   → {nom} (#{num or '?'})")

        elif "joueurs" in hs:
            print(f"\n[ROSTER] Table JOUEURS")
            for tr in table.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                nom = _extract_name_from_cell(tds[0])
                num = _extract_number_from_cell(tds[0], tds[1])
                if nom:
                    joueurs.append({
                        "nom": nom,
                        "numero": num,
                        "stats": dict(zip(headers, [td.get_text(strip=True) for td in tds]))
                    })
                    print(f"   → {nom} (#{num or '?'})")

        elif any("personnel" in h.lower() for h in headers):
            print(f"\n[ROSTER] Table PERSONNEL")
            for tr in table.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                nom = ""
                sp = tds[0].find("span", class_=re.compile("text-truncate", re.I))
                if sp:
                    nom = sp.get_text(strip=True)
                else:
                    div = tds[0].find("div", attrs={"title": True})
                    if div:
                        nom = div.get("title", "").strip()
                role = tds[1].get_text(strip=True)
                if nom:
                    personnel.append({"nom": nom, "role": role})
                    print(f"   → {nom} ({role})")

    print(f"\n[PARSING ROSTER] {len(gardiens)} G, {len(joueurs)} J, {len(personnel)} P.")
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


def parse_standings(html: str):
    """Classement de la division de l'équipe + rang dans la ligue.
    Team's division standings + rank in the whole league."""
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if "Équipe" not in headers:
            continue
        rows = []
        for tr in table.select("tbody tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < len(headers):
                continue
            row = dict(zip(headers, tds))
            img = tr.find("img")
            remote = img.get("src", "") if img else ""
            row["logo_remote"] = remote
            row["logo_local"] = download_logo(remote, row.get("Équipe", "")) if remote else ""
            rows.append(row)
        if any(MY_TEAM_NAME in r.get("Équipe", "").upper() for r in rows):
            tables.append(rows)
    if not tables:
        print("[PARSING STANDINGS] Équipe introuvable dans le classement.")
        return [], None, None, []
    tables.sort(key=len)
    division, ligue = tables[0], tables[-1]
    rang_ligue = next((r.get("#") for r in ligue if MY_TEAM_NAME in r.get("Équipe", "").upper()), None)
    print(f"[PARSING STANDINGS] Division: {len(division)} équipes, ligue: {len(ligue)} équipes.")
    # Version allégée de la ligue complète (sans logo distant) pour limiter la taille de l'attribut.
    # Lighter copy of the whole league (no remote logo) to keep the attribute small.
    ligue_courte = [{k: r.get(k) for k in ("#", "Équipe", "MJ", "V", "D", "N", "PTS", "logo_local")} for r in ligue]
    return division, rang_ligue, len(ligue), ligue_courte


# ============================================================
# MAIN
# ============================================================

async def scrape_and_publish():
    print(f"\n{'='*60}")
    print(f"[START] RseqHockey v3.4.0 - {MY_TEAM_NAME}")
    print(f"{'='*60}")

    base_url = f"https://scolaire.rseqhockey.com/fr/schedule-stats-standings/{LEAGUE_UUID}"
    team_url = f"https://scolaire.rseqhockey.com/fr/teams/{TEAM_ID}"

    timeout = aiohttp.ClientTimeout(total=FLARESOLVERR_TIMEOUT + 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        cookies, user_agent = await get_cloudflare_cookies(session)
        if not cookies:
            print("[ABORT] Impossible d'obtenir les cookies Cloudflare.")
            return

        schedule_html = await fetch_schedule_with_playwright(
            f"{team_url}?tab=schedule", cookies, user_agent
        )

        roster_html = await fetch_with_flaresolverr(session, f"{team_url}?tab=roster", "roster")
        standings_html = await fetch_with_flaresolverr(
            session,
            f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=standings",
            "standings"
        )
        stats_html = await fetch_with_flaresolverr(
            session,
            f"{base_url}?categoryId={LEAGUE_UUID}&scheduleId={SCHEDULE_SAISON_REGULIERE}&tab=playerstats",
            "stats_saison_reguliere"
        )

    print("\n[PARSING]...")
    matchs = merge_schedule(schedule_html) if schedule_html else []
    DERNIERS_MATCHS[:] = matchs
    roster_data = parse_roster_structured(roster_html) if roster_html else {"gardiens": [], "joueurs": [], "personnel": []}
    player_stats_sr = parse_table_generic(stats_html, "stats_saison_reguliere") if stats_html else []
    division, rang_ligue, nb_ligue, ligue = parse_standings(standings_html) if standings_html else ([], None, None, [])
    mon_equipe = next((r for r in division if MY_TEAM_NAME in r.get("Équipe", "").upper()), None)

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

    total_joueurs = len(roster_data["joueurs"]) + len(roster_data["gardiens"])
    publish_config(
        f"homeassistant/sensor/rseqhockey_{TEAM_ID}_roster/config",
        build_config(f"RSEQ Alignement", f"rseqhockey_{TEAM_ID}_roster",
                     f"rseqhockey/{TEAM_ID}/roster/state",
                     f"rseqhockey/{TEAM_ID}/roster/attributes", "mdi:account-group"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/state",
                    f"{total_joueurs} joueurs | {len(roster_data['personnel'])} instructeurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/roster/attributes", roster_data)

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
        build_config(f"RSEQ Stats Saison", f"rseqhockey_{TEAM_ID}_stats_saison_reguliere",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state",
                     f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", "mdi:hockey-sticks"))
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/state", f"{len(player_stats_sr)} joueurs")
    publish_to_mqtt(f"rseqhockey/{TEAM_ID}/stats_saison_reguliere/attributes", {"joueurs": player_stats_sr})

    if division:
        publish_config(
            f"homeassistant/sensor/rseqhockey_{TEAM_ID}_standings/config",
            build_config(f"RSEQ Classement", f"rseqhockey_{TEAM_ID}_standings",
                         f"rseqhockey/{TEAM_ID}/standings/state",
                         f"rseqhockey/{TEAM_ID}/standings/attributes", "mdi:trophy"))
        publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/state",
                        f"{mon_equipe.get('#', '?')}e / {len(division)}" if mon_equipe else "Hors classement")
        publish_to_mqtt(f"rseqhockey/{TEAM_ID}/standings/attributes", {
            "mon_equipe": mon_equipe,
            "classement_complet": division,
            "rang_ligue": rang_ligue,
            "nb_equipes_ligue": nb_ligue,
            "classement_ligue": ligue,
        })

    print("\n[FIN] Cycle terminé.")


if __name__ == "__main__":
    while True:
        asyncio.run(scrape_and_publish())
        # Rythme adaptatif les jours de match (rythme.py) / adaptive rhythm on game days
        attente, raison = rythme.delai(
            [(f"{m.get('iso')}|{'/'.join(m.get('equipes', []))}", rythme.lire_iso(m.get("iso")), bool(m.get("joue")))
             for m in DERNIERS_MATCHS], INTERVAL)
        print(f"\n[ATTENTE] {attente / 60:.0f} min ({raison})...")
        time.sleep(attente)
