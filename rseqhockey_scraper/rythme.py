"""Rythme de mise a jour adaptatif les jours de match (commun aux modules hockey et basket).
Adaptive update rhythm on game days (shared by the hockey and basket add-ons).

- avant un match : reveil a l'heure du debut / before a game: wake up at start time
- pendant le match : toutes les 10 min, jusqu'au resultat (4 h au plus) / during: every 10 min until the result (4 h max)
- apres le match : toutes les heures pendant 3 h / after: hourly for 3 h
- sinon : intervalle normal du module / otherwise: the add-on's normal interval
"""
import json
from datetime import datetime, timedelta, timezone

ETAT = "/data/rythme_fins.json"      # heure de fin detectee par match / detected end time per game
DEBUTS = "/data/debuts_matchs.json"  # heure de debut vue avant le match / start time seen before the game
EN_JEU = ("EN COURS", "PROLONGATION", "FUSILLADE")
EN_MATCH = timedelta(minutes=10)
APRES = timedelta(hours=1)
APRES_DUREE = timedelta(hours=3)
MAX_MATCH = timedelta(hours=4)       # sans resultat apres 4 h, le match est considere termine
RECENT = timedelta(hours=12)         # seuls les matchs commences depuis moins de 12 h comptent


def lire_iso(txt, tz_locale=None):
    """ISO -> datetime avec fuseau (« Z » accepte ; sans fuseau = heure locale tz_locale, sinon UTC)."""
    if not txt:
        return None
    try:
        d = datetime.fromisoformat(str(txt).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz_locale or timezone.utc)
    return d


def cle_match(m):
    """Cle stable d'un match (date + equipes) / stable game key."""
    return f"{m.get('date_iso')}|{'/'.join(m.get('equipes', []))}"


def histo_valide(m):
    """Match a garder dans l'historique : joue, FINAL et date connue (purge les anciennes entrees
    « Aujourd'hui … EN COURS 0-0 »). / History entry: played, FINAL and dated."""
    return bool(m.get("joue")) and bool(m.get("date_iso")) and str(m.get("statut", "FINAL")).upper() == "FINAL"


def garder_debuts(matchs, fichier=DEBUTS):
    """La page retire l'heure du match une fois commence : on remet celle vue avant.
    The page drops the start time once the game starts: restore the one seen earlier."""
    try:
        with open(fichier, encoding="utf-8") as f:
            debuts = json.load(f)
    except Exception:
        debuts = {}
    for m in matchs:
        cle = cle_match(m)
        if not m.get("date_iso"):
            continue
        if m.get("iso"):
            debuts[cle] = [m["iso"], m.get("heure", "")]
        elif cle in debuts:
            m["iso"] = debuts[cle][0]
            m["heure"] = m.get("heure") or debuts[cle][1]
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    debuts = {k: v for k, v in debuts.items() if k[:10] >= limite}
    try:
        with open(fichier, "w", encoding="utf-8") as f:
            json.dump(debuts, f)
    except Exception:
        pass
    return matchs


def entree(m, maintenant=None):
    """(cle, debut, joue) pour delai() ; un match en cours sans heure connue compte comme commence.
    Entry for delai(); a live game with no known start time counts as started now."""
    debut = lire_iso(m.get("iso"))
    if debut is None and not m.get("joue") and str(m.get("statut", "")).upper() in EN_JEU:
        debut = maintenant or datetime.now(timezone.utc)
    return cle_match(m), debut, bool(m.get("joue"))


def delai(matchs, normal_s, maintenant=None, etat=ETAT):
    """matchs : liste de (cle, debut datetime avec fuseau, joue bool). Retourne (secondes, raison)."""
    now = maintenant or datetime.now(timezone.utc)
    try:
        with open(etat, encoding="utf-8") as f:
            fins = json.load(f)
    except Exception:
        fins = {}
    choix = [(float(normal_s), "intervalle normal")]
    for cle, debut, joue in matchs:
        if debut is None:
            continue
        if now < debut:
            choix.append(((debut - now).total_seconds(), f"début du match {cle}"))
            continue
        if now - debut > RECENT:
            continue
        fin = lire_iso(fins.get(cle))
        if fin is None and (joue or now >= debut + MAX_MATCH):
            fin = now if joue else debut + MAX_MATCH
            fins[cle] = fin.isoformat()
        if fin is None:
            choix.append((EN_MATCH.total_seconds(), f"match en cours {cle}"))
        elif now < fin + APRES_DUREE:
            restant = (fin + APRES_DUREE - now).total_seconds()
            choix.append((min(APRES.total_seconds(), restant), f"après le match {cle}"))
    fins = {k: v for k, v in fins.items() if lire_iso(v) and now - lire_iso(v) < timedelta(days=2)}
    try:
        with open(etat, "w", encoding="utf-8") as f:
            json.dump(fins, f)
    except Exception:
        pass
    s, raison = min(choix, key=lambda c: c[0])
    return max(60, int(s)), raison
