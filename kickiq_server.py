"""
Serveur KickIQ (gratuit) : lancé automatiquement toutes les 5 minutes par GitHub Actions.

Sources de données (toutes gratuites et officielles) :
  - football-data.org : matchs, scores, classements, équipes, effectifs, buteurs (12 compétitions) ;
  - OpenLigaDB        : Bundesliga et 2. Bundesliga, avec les buteurs minute par minute, saisis
                        en direct par une communauté de bénévoles (pas de clé nécessaire).

Ce qu'il fait à chaque passage :
  1. récupère les matchs des 12 compétitions gratuites de football-data.org (J-3 à J+13) ;
  2. les enregistre dans Firestore (une fiche par jour) pour que l'appli les affiche ;
  3. met à jour les classements des championnats (une fois par jour) ;
  4. crée et note les journées de Fantasy ;
  5. note les pronostics des matchs terminés et met à jour les classements de pronostics ;
  6. calcule les statistiques « que pronostiquent les autres ? » ;
  7. envoie les notifications des défis entre amis ;
  8. masque les contenus signalés plusieurs fois ou contenant des insultes ;
  9. (V4) met à jour les fiches des équipes, les effectifs, les buteurs et les derniers résultats ;
 10. (V4) « mode direct » : quand des matchs sont en cours, rafraîchit les scores chaque minute
     pendant quelques minutes (variable LIVE_PASSES, 0 pour désactiver).

Variables d'environnement (secrets GitHub) :
  FOOTBALL_DATA_TOKEN       clé gratuite de football-data.org
  FIREBASE_SERVICE_ACCOUNT  contenu du fichier JSON « compte de service » de Firebase
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

import logic

FREE_COMPETITIONS = ["PL", "CL", "PD", "SA", "BL1", "FL1", "DED", "PPL", "ELC", "BSA", "WC", "EC"]
FANTASY_COMPETITIONS = {"PL", "CL", "PD", "SA", "BL1", "FL1", "DED", "PPL", "BSA", "WC", "EC"}

LOG = []


def log(*parts):
    line = " ".join(str(p) for p in parts)
    LOG.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# football-data.org
# ---------------------------------------------------------------------------
class FootballData:
    BASE = "https://api.football-data.org/v4"

    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers["X-Auth-Token"] = token
        self.calls = 0

    def get(self, path, params=None):
        # Limite gratuite : 10 appels par minute. On espace les appels après les deux premiers.
        if self.calls >= 2:
            time.sleep(6.5)
        self.calls += 1
        for attempt in range(2):
            r = self.session.get(self.BASE + path, params=params, timeout=30)
            if r.status_code == 429 and attempt == 0:
                log("  limite d'appels atteinte, pause de 60 s")
                time.sleep(60)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def matches(self, date_from, date_to):
        data = self.get("/matches", {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()})
        return data.get("matches", [])

    def standings(self, code):
        return self.get(f"/competitions/{code}/standings")

    def match(self, match_id):
        data = self.get(f"/matches/{match_id}")
        return data.get("match", data)

    def teams(self, code):
        return self.get(f"/competitions/{code}/teams").get("teams", [])

    def scorers(self, code, limit=30):
        return self.get(f"/competitions/{code}/scorers", {"limit": limit}).get("scorers", [])


class OpenLiga:
    """OpenLigaDB : données libres de la Bundesliga, sans clé. Une panne ici n'arrête jamais le serveur."""
    BASE = "https://api.openligadb.de"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "KickIQ-server (projet amateur)"
        self.calls = 0

    def get(self, path):
        if self.calls:
            time.sleep(0.5)  # par politesse envers un service gratuit
        self.calls += 1
        r = self.session.get(self.BASE + path, timeout=30)
        r.raise_for_status()
        return r.json()

    def season(self, league, year):
        return self.get(f"/getmatchdata/{league}/{year}") or []

    def current(self, league):
        return self.get(f"/getmatchdata/{league}") or []

    def table(self, league, year):
        return self.get(f"/getbltable/{league}/{year}") or []

    def scorers(self, league, year):
        return self.get(f"/getgoalgetters/{league}/{year}") or []


# ---------------------------------------------------------------------------
# Outils Firestore
# ---------------------------------------------------------------------------
class Writer:
    """Regroupe les écritures par lots de 400 (limite Firestore : 500)."""

    def __init__(self, db):
        self.db = db
        self.batch = db.batch()
        self.count = 0
        self.total = 0

    def set(self, ref, data, merge=False):
        self.batch.set(ref, data, merge=merge)
        self._tick()

    def update(self, ref, data):
        self.batch.update(ref, data)
        self._tick()

    def _tick(self):
        self.count += 1
        self.total += 1
        if self.count >= 400:
            self.flush()

    def flush(self):
        if self.count:
            self.batch.commit()
            self.batch = self.db.batch()
            self.count = 0


class Users:
    """Cache des profils publics et des appareils (pour les notifications)."""

    def __init__(self, db):
        self.db = db
        self.profiles = {}
        self.devices = {}

    def profile(self, uid):
        if uid not in self.profiles:
            doc = self.db.collection("users").document(uid).get()
            d = doc.to_dict() or {}
            self.profiles[uid] = {
                "pseudo": d.get("pseudo", "?"),
                "avatar": int(d.get("avatar", 0)),
                "country": (d.get("country") or "").upper(),
            }
        return self.profiles[uid]

    def device(self, uid):
        if uid not in self.devices:
            doc = self.db.collection("users").document(uid).collection("private").document("device").get()
            self.devices[uid] = doc.to_dict() if doc.exists else None
        return self.devices[uid]

    def notify(self, uid, kind, data=None, **kw):
        dev = self.device(uid)
        if not dev or not dev.get("token"):
            return
        title, body = logic.message(kind, dev.get("lang", "en"), **kw)
        try:
            messaging.send(messaging.Message(
                token=dev["token"],
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {"type": kind}).items()},
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(channel_id="kickiq_main"),
                ),
            ))
        except messaging.UnregisteredError:
            # L'appli a été désinstallée : on oublie ce jeton.
            self.db.collection("users").document(uid).collection("private").document("device").delete()
        except Exception as e:  # une notification ratée ne doit jamais arrêter le serveur
            log("  notification impossible :", type(e).__name__)


# ---------------------------------------------------------------------------
# 1-2. Matchs
# ---------------------------------------------------------------------------
def save_matchdays(db, matches, today, only_days=None):
    by_day = defaultdict(dict)
    for m in matches:
        by_day[logic.day_key(m["k"])][str(m["i"])] = m
    first, last = today - timedelta(days=3), today + timedelta(days=13)
    changed = 0
    d = first
    while d <= last:
        key = d.isoformat()
        if only_days is not None and key not in only_days:
            d += timedelta(days=1)
            continue
        ref = db.collection("matchdays").document(key)
        new_m = by_day.get(key, {})
        old = ref.get()
        if not old.exists or (old.to_dict() or {}).get("m") != new_m:
            ref.set({"date": key, "m": new_m, "updatedAt": firestore.SERVER_TIMESTAMP})
            changed += 1
        d += timedelta(days=1)
    if only_days is None or changed:
        log(f"Matchs : {len(matches)} matchs, {changed} jour(s) mis à jour")


# ---------------------------------------------------------------------------
# 1 bis. OpenLigaDB : buteurs de Bundesliga et matchs de 2. Bundesliga
# ---------------------------------------------------------------------------
def apply_openliga(matches, names, ol_data, today, now_ms):
    """Ajoute les buteurs (et le score en direct) aux matchs de Bundesliga, et les matchs de 2. Bundesliga.

    names   : {id match football-data: (nom long domicile, nom long extérieur)}
    ol_data : {"BL1": [matchs OpenLigaDB], "BL2": [...]}
    """
    first = datetime.combine(today - timedelta(days=3), datetime.min.time(), tzinfo=timezone.utc)
    last = first + timedelta(days=17)
    lo, hi = int(first.timestamp() * 1000), int(last.timestamp() * 1000)
    out = []
    bl1 = [m for m in matches if m["c"] == "BL1"]
    found = logic.match_openliga([(m["i"], m["k"], *names.get(m["i"], (m["h"], m["a"]))) for m in bl1],
                                 ol_data.get("BL1", []))
    enriched = 0
    for m in matches:
        if m["c"] == "BL1" and m["i"] in found:
            m = logic.enrich_with_openliga(m, found[m["i"]], now_ms)
            enriched += 1
        out.append(m)
    added = 0
    for om in ol_data.get("BL2", []):
        cm = logic.compact_openliga_match(om, "BL2", "2. Bundesliga", now_ms)
        if cm and lo <= cm["k"] < hi:
            out.append(cm)
            added += 1
    if enriched or added:
        log(f"OpenLigaDB : {enriched} match(s) de Bundesliga enrichi(s), {added} match(s) de 2. Bundesliga")
    return out


def fetch_openliga(ol, today, current_only=False):
    data = {}
    for code, league in logic.OPENLIGA_LEAGUES.items():
        try:
            data[code] = ol.current(league) if current_only else ol.season(league, logic.season_year(today))
        except Exception as e:
            log(f"  OpenLigaDB {league} indisponible ({type(e).__name__})")
    return data


# ---------------------------------------------------------------------------
# 3. Classements des championnats (une fois par jour)
# ---------------------------------------------------------------------------
def update_standings(db, api, now, ol=None):
    meta_ref = db.collection("standings").document("_meta")
    meta = meta_ref.get().to_dict() or {}
    last = meta.get("updatedAt")
    if last and (now - last).total_seconds() < 20 * 3600:
        return
    done = 0
    for code in FREE_COMPETITIONS:
        try:
            data = api.standings(code)
        except Exception as e:
            log(f"  classement {code} indisponible ({type(e).__name__})")
            continue
        tables = []
        for st in data.get("standings", []):
            if st.get("type") != "TOTAL":
                continue
            rows = [{
                "p": r.get("position"), "id": (r.get("team") or {}).get("id"),
                "t": (r.get("team") or {}).get("shortName") or (r.get("team") or {}).get("name"),
                "pl": r.get("playedGames"), "w": r.get("won"), "d": r.get("draw"), "l": r.get("lost"),
                "gf": r.get("goalsFor"), "ga": r.get("goalsAgainst"), "gd": r.get("goalDifference"),
                "pts": r.get("points"),
                **({"fo": r["form"].replace(",", "")[-5:]} if r.get("form") else {}),
            } for r in st.get("table", [])]
            tables.append({"g": st.get("group") or "", "rows": rows})
        if not tables:
            continue
        comp = data.get("competition") or {}
        season = (data.get("season") or {}).get("startDate", "")[:4]
        db.collection("standings").document(code).set({
            "code": code, "name": comp.get("name", code), "season": season,
            "tables": tables, "updatedAt": firestore.SERVER_TIMESTAMP,
        })
        done += 1
    if ol is not None:
        year = logic.season_year(now.date())
        try:
            rows = logic.openliga_table(ol.table("bl2", year), id_offset=logic.OPENLIGA_ID_OFFSET)
            if rows:
                db.collection("standings").document("BL2").set({
                    "code": "BL2", "name": "2. Bundesliga", "season": str(year),
                    "tables": [{"g": "", "rows": rows}], "updatedAt": firestore.SERVER_TIMESTAMP,
                })
                done += 1
        except Exception as e:
            log(f"  classement BL2 indisponible ({type(e).__name__})")
    meta_ref.set({"updatedAt": firestore.SERVER_TIMESTAMP})
    log(f"Classements : {done} compétition(s) mises à jour")


def load_prices(db):
    """teamId -> (position, taille du tableau), d'après les classements enregistrés."""
    prices = {}
    for doc in db.collection("standings").stream():
        if doc.id == "_meta":
            continue
        for table in (doc.to_dict() or {}).get("tables", []):
            rows = table.get("rows", [])
            for r in rows:
                if r.get("id"):
                    prices[(doc.id, r["id"])] = logic.team_price(r.get("p"), len(rows))
    return prices


# ---------------------------------------------------------------------------
# 9. Fiches des équipes, effectifs, buteurs, derniers résultats (V4)
# ---------------------------------------------------------------------------
TEAMS_EVERY = timedelta(days=3)       # les effectifs changent peu
SCORERS_EVERY = timedelta(hours=6)
NOT_FREE_RETRY = timedelta(days=7)    # si la formule gratuite refuse une donnée, on réessaie dans 7 jours


def _stalest(meta, codes, every, now):
    """La compétition dont la donnée est la plus ancienne (et plus vieille que « every »), ou None."""
    def age(code):
        t = meta.get(code)
        return t if t else datetime(2000, 1, 1, tzinfo=timezone.utc)
    code = min(codes, key=age)
    return code if now - age(code) >= every else None


def update_stats(db, api, ol, now):
    """Une compétition par passage pour les équipes, une pour les buteurs : le quota gratuit est respecté."""
    meta_ref = db.collection("stats").document("_meta")
    meta = meta_ref.get().to_dict() or {}
    teams_meta, scorers_meta = dict(meta.get("teams", {})), dict(meta.get("scorers", {}))

    code = _stalest(teams_meta, FREE_COMPETITIONS, TEAMS_EVERY, now)
    if code:
        try:
            teams = api.teams(code)
            w = Writer(db)
            for t in teams:
                if not t.get("id"):
                    continue
                doc = logic.compact_team(t, code)
                doc["comps"] = firestore.ArrayUnion([code])
                doc["updatedAt"] = firestore.SERVER_TIMESTAMP
                w.set(db.collection("teams").document(str(t["id"])), doc, merge=True)
            w.flush()
            teams_meta[code] = now
            log(f"Équipes : {len(teams)} fiche(s) {code} mises à jour")
        except requests.HTTPError as e:
            teams_meta[code] = now - TEAMS_EVERY + NOT_FREE_RETRY
            log(f"  équipes {code} indisponibles (HTTP {e.response.status_code if e.response is not None else '?'})")

    code = _stalest(scorers_meta, FREE_COMPETITIONS + ["BL2"], SCORERS_EVERY, now)
    if code:
        rows = None
        try:
            if code == "BL2":
                rows = logic.openliga_scorers(ol.scorers("bl2", logic.season_year(now.date())))
            else:
                rows = [logic.compact_scorer(x) for x in api.scorers(code)]
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if code == "BL1" and status in (401, 403):
                # Repli : buteurs de Bundesliga d'après OpenLigaDB
                try:
                    rows = logic.openliga_scorers(ol.scorers("bl1", logic.season_year(now.date())))
                except Exception:
                    rows = None
            if rows is None:
                scorers_meta[code] = now - SCORERS_EVERY + NOT_FREE_RETRY
                log(f"  buteurs {code} indisponibles (HTTP {status})")
        except Exception as e:
            scorers_meta[code] = now - SCORERS_EVERY + timedelta(hours=1)
            log(f"  buteurs {code} indisponibles ({type(e).__name__})")
        if rows is not None:
            db.collection("scorers").document(code).set({
                "code": code, "rows": rows, "updatedAt": firestore.SERVER_TIMESTAMP,
            })
            scorers_meta[code] = now
            log(f"Buteurs : {code} ({len(rows)} joueur(s))")

    meta_ref.set({"teams": teams_meta, "scorers": scorers_meta})


def update_recent(db, matches):
    """Ajoute chaque match terminé aux « derniers résultats » des deux équipes (une seule fois par match)."""
    cache = {}
    w = Writer(db)
    added = 0
    for m in matches:
        entries = logic.recent_entries(m)
        if not entries:
            continue
        day = logic.day_key(m["k"])
        if day not in cache:
            cache[day] = (db.collection("matchstats").document(day).get().to_dict() or {}).get("recent", {})
        if cache[day].get(str(m["i"])):
            continue
        for tid, last in entries.items():
            if not tid:
                continue
            name = m["h"] if tid == m["hi"] else m["a"]
            w.set(db.collection("teams").document(str(tid)),
                  {"id": tid, "sn": name, "last": last, "comps": firestore.ArrayUnion([m["c"]])}, merge=True)
        w.set(db.collection("matchstats").document(day), {"recent": {str(m["i"]): True}}, merge=True)
        cache[day][str(m["i"])] = True
        added += 1
    w.flush()
    if added:
        log(f"Derniers résultats : {added} match(s) ajouté(s) aux fiches des équipes")


# ---------------------------------------------------------------------------
# 10. Mode direct
# ---------------------------------------------------------------------------
LIVE_PASSES = int(os.environ.get("LIVE_PASSES", "3"))
LIVE_PAUSE = 60  # secondes entre deux rafraîchissements


def live_mode(db, api, ol, matches, names, users):
    """Pendant les matchs : quelques rafraîchissements rapprochés des scores du jour."""
    for n in range(LIVE_PASSES):
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        if not logic.needs_live_mode(matches, now_ms):
            return matches
        time.sleep(LIVE_PAUSE)
        now = datetime.now(timezone.utc)
        today = now.date()
        now_ms = int(now.timestamp() * 1000)
        start = today - timedelta(days=1) if now.hour < 4 else today
        try:
            raw = api.matches(start, today)
        except Exception as e:
            log(f"  direct : football-data indisponible ({type(e).__name__})")
            raw = []
        for m in raw:
            if m.get("homeTeam") and m.get("awayTeam"):
                names[m["id"]] = ((m["homeTeam"] or {}).get("name") or "", (m["awayTeam"] or {}).get("name") or "")
        fresh = [logic.compact_match(m) for m in raw if m.get("utcDate")]
        by_id = {m["i"]: m for m in matches}
        live_codes = {m["c"] for m in matches if m["s"] in ("LIVE", "HT")
                      or (m["s"] == "NS" and m["k"] <= now_ms + 10 * 60 * 1000 and m["k"] >= now_ms - 3 * 3600 * 1000)}
        for m in fresh:
            by_id[m["i"]] = m
        merged = list(by_id.values())
        # garder les buteurs déjà connus pour les matchs rafraîchis
        old_by_id = {o["i"]: o for o in matches}
        for m in merged:
            old = old_by_id.get(m["i"])
            if old and "ev" in old and "ev" not in m:
                m["ev"], m["ol"] = old["ev"], old.get("ol")
        if live_codes & set(logic.OPENLIGA_LEAGUES):
            ol_now = fetch_openliga(ol, today, current_only=True)
            # Les matchs de 2. Bundesliga de la journée en cours sont remplacés par leur version fraîche.
            fresh_bl2 = {logic.OPENLIGA_ID_OFFSET + int(logic._get(om, "matchID", "MatchID") or 0)
                         for om in ol_now.get("BL2", [])}
            merged = apply_openliga([m for m in merged if m["i"] not in fresh_bl2], names, ol_now, today, now_ms)
        matches = merged
        days = {today.isoformat(), start.isoformat()}
        save_matchdays(db, matches, today, only_days=days)
        score_predictions(db, matches, users)
        log(f"Direct : passage {n + 1}/{LIVE_PASSES}")
    return matches


# ---------------------------------------------------------------------------
# 4. Fantasy
# ---------------------------------------------------------------------------
def update_fantasy(db, api, matches, users, now_ms):
    prices = load_prices(db)
    groups = defaultdict(list)
    for m in matches:
        if m["c"] in FANTASY_COMPETITIONS and m["se"]:
            groups[(m["c"], m["se"], logic.round_key(m))].append(m)

    created = 0
    for (comp, season, key), ms in groups.items():
        rid = logic.round_id(comp, season, key)
        ref = db.collection("fantasy").document(rid)
        snap = ref.get()
        deadline = min(m["k"] for m in ms)
        match_map = {str(m["i"]): {k: m[k] for k in ("h", "a", "hi", "ai", "hs", "as", "s", "k")} for m in ms}
        if not snap.exists:
            if deadline < now_ms + 3600 * 1000:
                continue  # trop tard pour ouvrir cette journée
            teams = {}
            for m in ms:
                for tid, name in ((m["hi"], m["h"]), (m["ai"], m["a"])):
                    teams[str(tid)] = {"n": name, "p": prices.get((comp, tid), logic.PRICE_DEFAULT)}
            if len(teams) < logic.FANTASY_SQUAD:
                continue
            ref.set({
                "comp": comp, "compName": ms[0]["cn"], "season": season, "key": key, "md": ms[0]["md"],
                "deadline": deadline, "status": "open", "teams": teams, "matches": match_map,
                "budget": logic.FANTASY_BUDGET, "squad": logic.FANTASY_SQUAD,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            })
            created += 1
        else:
            data = snap.to_dict() or {}
            if data.get("status") == "done":
                continue
            merged = dict(data.get("matches", {}))
            merged.update(match_map)
            update = {"matches": merged, "updatedAt": firestore.SERVER_TIMESTAMP}
            if data.get("status") == "open":
                # nouveaux matchs de la même journée apparus dans la fenêtre : on ajoute les clubs
                teams = dict(data.get("teams", {}))
                for m in ms:
                    for tid, name in ((m["hi"], m["h"]), (m["ai"], m["a"])):
                        teams.setdefault(str(tid), {"n": name, "p": prices.get((comp, tid), logic.PRICE_DEFAULT)})
                update["teams"] = teams
                if now_ms >= data.get("deadline", deadline):
                    update["status"] = "locked"
            ref.update(update)
    if created:
        log(f"Fantasy : {created} journée(s) ouverte(s)")

    # Journées verrouillées : on vérifie si tous les matchs sont terminés, puis on note.
    in_window = {str(m["i"]): m for m in matches}
    fallback_calls = 0
    for snap in db.collection("fantasy").where(filter=FieldFilter("status", "in", ["open", "locked"])).stream():
        data = snap.to_dict() or {}
        if now_ms < data.get("deadline", 0):
            continue
        ms = dict(data.get("matches", {}))
        for mid, m in list(ms.items()):
            if m["s"] in logic.FINAL or m["s"] in logic.VOID:
                continue
            if mid in in_window:
                fresh = in_window[mid]
            elif fallback_calls < 5:
                fallback_calls += 1
                try:
                    fresh = logic.compact_match(api.match(mid))
                except Exception:
                    continue
            else:
                continue
            ms[mid] = {k: fresh[k] for k in ("h", "a", "hi", "ai", "hs", "as", "s", "k")}
        all_over = all(m["s"] in logic.FINAL or m["s"] in logic.VOID for m in ms.values())
        if not all_over:
            snap.reference.update({"matches": ms, "status": "locked"})
            continue
        score_fantasy_round(db, snap.reference, data, ms, users)


def score_fantasy_round(db, ref, data, matches, users):
    team_points = defaultdict(int)
    for m in matches.values():
        if m["s"] not in logic.FINAL or m["hs"] is None:
            continue
        team_points[str(m["hi"])] += logic.team_match_points(m["hs"], m["as"])
        team_points[str(m["ai"])] += logic.team_match_points(m["as"], m["hs"])
    rid = ref.id
    season_board = f"fan_{data['comp']}-{data['season']}_world"
    w = Writer(db)
    scored = 0
    for e in ref.collection("entries").stream():
        entry = e.to_dict() or {}
        if entry.get("done"):
            continue
        pts = logic.fantasy_entry_points(entry.get("picks", []), entry.get("cap"), team_points)
        uid = e.id
        prof = users.profile(uid)
        w.update(e.reference, {"pts": pts, "done": True})
        base = {"uid": uid, "pseudo": prof["pseudo"], "avatar": prof["avatar"], "country": prof["country"],
                "updatedAt": firestore.SERVER_TIMESTAMP}
        w.set(db.collection("boards").document(f"fan_{rid}_world").collection("entries").document(uid),
              {**base, "points": pts})
        w.set(db.collection("boards").document(season_board).collection("entries").document(uid),
              {**base, "points": firestore.Increment(pts), "n": firestore.Increment(1)}, merge=True)
        scored += 1
        users.notify(uid, "fantasy", {"type": "fantasy", "round": rid}, comp=data.get("compName", data["comp"]), pts=pts)
    w.flush()
    ref.update({"matches": matches, "status": "done", "teamPoints": dict(team_points),
                "scoredAt": firestore.SERVER_TIMESTAMP})
    log(f"Fantasy : journée {rid} notée ({scored} équipe(s))")


# ---------------------------------------------------------------------------
# 5. Pronostics
# ---------------------------------------------------------------------------
def score_predictions(db, matches, users):
    finals = [m for m in matches if m["s"] in logic.FINAL and m["hs"] is not None and m["as"] is not None]
    stats_cache = {}
    w = Writer(db)
    total = 0
    for m in finals:
        day = logic.day_key(m["k"])
        if day not in stats_cache:
            stats_cache[day] = (db.collection("matchstats").document(day).get().to_dict() or {})
        if stats_cache[day].get("scored", {}).get(str(m["i"])):
            continue
        d = date.fromisoformat(day)
        q = db.collection("predictions").where(filter=FieldFilter("mid", "==", str(m["i"])))
        for p in q.stream():
            pred = p.to_dict() or {}
            if pred.get("done"):
                continue
            uid = pred.get("uid")
            if not uid:
                continue
            pts = logic.prediction_points(int(pred.get("h", 0)), int(pred.get("a", 0)), m["hs"], m["as"])
            prof = users.profile(uid)
            w.update(p.reference, {"pts": pts, "done": True})
            for bid in logic.board_ids("pred_", d, prof["country"]):
                w.set(db.collection("boards").document(bid).collection("entries").document(uid), {
                    "uid": uid, "pseudo": prof["pseudo"], "avatar": prof["avatar"], "country": prof["country"],
                    "points": firestore.Increment(pts), "n": firestore.Increment(1),
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                }, merge=True)
            total += 1
            users.notify(uid, "pred", {"type": "pred", "mid": m["i"]},
                         h=m["h"], a=m["a"], rh=m["hs"], ra=m["as"], pts=pts)
        w.flush()  # la note et les points d'un même pronostic partent ensemble
        db.collection("matchstats").document(day).set({"scored": {str(m["i"]): True}}, merge=True)
        stats_cache[day].setdefault("scored", {})[str(m["i"])] = True
    w.flush()
    if total:
        log(f"Pronostics : {total} pronostic(s) noté(s)")


# ---------------------------------------------------------------------------
# 6. Statistiques « que pronostiquent les autres ? »
# ---------------------------------------------------------------------------
def prediction_stats(db, today):
    for offset in range(-1, 8):
        day = (today + timedelta(days=offset)).isoformat()
        counts = defaultdict(lambda: {"n": 0, "h": 0, "d": 0, "a": 0})
        for p in db.collection("predictions").where(filter=FieldFilter("date", "==", day)).stream():
            pred = p.to_dict() or {}
            c = counts[pred.get("mid", "?")]
            c["n"] += 1
            diff = int(pred.get("h", 0)) - int(pred.get("a", 0))
            c["h" if diff > 0 else "a" if diff < 0 else "d"] += 1
        if counts:
            db.collection("matchstats").document(day).set({"stats": dict(counts)}, merge=True)


# ---------------------------------------------------------------------------
# 7. Défis entre amis
# ---------------------------------------------------------------------------
def process_challenges(db, users, now):
    since = now - timedelta(days=3)
    q = db.collection("challenges").where(filter=FieldFilter("createdAt", ">=", since))
    for c in q.stream():
        ch = c.to_dict() or {}
        upd = {}
        if not ch.get("nTo"):
            users.notify(ch["to"], "challenge_new", {"type": "challenge", "id": c.id}, name=ch.get("fromName", "?"))
            upd["nTo"] = True
        if ch.get("status") == "done" and not ch.get("nFrom"):
            users.notify(ch["from"], "challenge_done", {"type": "challenge", "id": c.id},
                         name=ch.get("toName", "?"), mine=ch.get("fromScore", 0), theirs=ch.get("toScore", 0))
            upd["nFrom"] = True
        exp = ch.get("expiresAt")
        if ch.get("status") == "pending" and exp and exp < now:
            upd["status"] = "expired"
        if upd:
            c.reference.update(upd)


# ---------------------------------------------------------------------------
# 8. Modération
# ---------------------------------------------------------------------------
def moderate(db, now):
    hidden = 0
    # Signalements : un contenu signalé par 3 personnes différentes est masqué.
    pending = list(db.collection("reports").where(filter=FieldFilter("processed", "==", False)).stream())
    targets = {r.to_dict().get("path") for r in pending if (r.to_dict() or {}).get("path")}
    for path in targets:
        reporters = {r.to_dict().get("by") for r in
                     db.collection("reports").where(filter=FieldFilter("path", "==", path)).stream()}
        if len(reporters) >= logic.REPORTS_TO_HIDE:
            ref = db.document(path)
            if ref.get().exists:
                ref.update({"hidden": True})
                hidden += 1
    for r in pending:
        r.reference.update({"processed": True})

    # Filtre de mots sur les publications récentes
    since = now - timedelta(days=1)
    for p in db.collection("posts").where(filter=FieldFilter("createdAt", ">=", since)).stream():
        post = p.to_dict() or {}
        if not post.get("hidden") and logic.is_offensive(post.get("text", "")):
            p.reference.update({"hidden": True})
            hidden += 1
    if hidden:
        log(f"Modération : {hidden} contenu(s) masqué(s)")


# ---------------------------------------------------------------------------
def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    account = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not token or not account:
        sys.exit("Il manque FOOTBALL_DATA_TOKEN ou FIREBASE_SERVICE_ACCOUNT (secrets GitHub).")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(account)))
    db = firestore.client()
    api = FootballData(token)
    users = Users(db)

    now = datetime.now(timezone.utc)
    today = now.date()
    now_ms = int(now.timestamp() * 1000)

    ol = OpenLiga()

    raw = []
    for a, b in logic.fetch_windows(today):
        raw += api.matches(a, b)
    names = {m["id"]: ((m.get("homeTeam") or {}).get("name") or "", (m.get("awayTeam") or {}).get("name") or "")
             for m in raw}
    matches = [logic.compact_match(m) for m in raw if m.get("utcDate")]
    try:
        matches = apply_openliga(matches, names, fetch_openliga(ol, today), today, now_ms)
    except Exception as e:  # la source complémentaire ne doit jamais bloquer la principale
        log(f"ERREUR OpenLigaDB : {type(e).__name__}: {e}")

    steps = [
        ("matchs", lambda: save_matchdays(db, matches, today)),
        ("classements", lambda: update_standings(db, api, now, ol)),
        ("équipes et buteurs", lambda: update_stats(db, api, ol, now)),
        ("derniers résultats", lambda: update_recent(db, matches)),
        ("fantasy", lambda: update_fantasy(db, api, matches, users, now_ms)),
        ("pronostics", lambda: score_predictions(db, matches, users)),
        ("défis", lambda: process_challenges(db, users, now)),
    ]
    # Étapes plus coûteuses en lectures Firestore : une fois sur trois (toutes les 15 minutes).
    if now.minute % 15 < 5:
        steps += [
            ("statistiques", lambda: prediction_stats(db, today)),
            ("modération", lambda: moderate(db, now)),
        ]
    failed = 0
    for name, step in steps:
        try:
            step()
        except Exception as e:  # une étape en erreur n'empêche pas les suivantes
            failed += 1
            log(f"ERREUR à l'étape {name} : {type(e).__name__}: {e}")
    try:
        live_mode(db, api, ol, matches, names, users)
    except Exception as e:
        failed += 1
        log(f"ERREUR en mode direct : {type(e).__name__}: {e}")
    log(f"Terminé ({api.calls} appel(s) à football-data.org, {ol.calls} à OpenLigaDB)")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
