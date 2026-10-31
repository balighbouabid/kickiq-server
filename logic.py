"""Règles du serveur KickIQ, sans dépendance réseau : facilement testables (voir test_logic.py)."""
import re
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Pronostics
# ---------------------------------------------------------------------------
PRED_EXACT = 10        # score exact
PRED_DIFF = 6          # bon vainqueur (ou nul) ET bon écart de buts
PRED_RESULT = 4        # bon vainqueur (ou nul) seulement


def _sign(x):
    return (x > 0) - (x < 0)


def prediction_points(ph, pa, rh, ra):
    """Points d'un pronostic (ph-pa) pour un résultat réel (rh-ra)."""
    if ph == rh and pa == ra:
        return PRED_EXACT
    if _sign(ph - pa) != _sign(rh - ra):
        return 0
    if ph - pa == rh - ra:
        return PRED_DIFF
    return PRED_RESULT


# ---------------------------------------------------------------------------
# Fantasy « équipes »
# ---------------------------------------------------------------------------
FANTASY_BUDGET = 100
FANTASY_SQUAD = 5
PRICE_MIN, PRICE_MAX, PRICE_DEFAULT = 10, 30, 20


def team_match_points(goals_for, goals_against):
    """Points d'un club pour un match : victoire 3, nul 1, +1 par but marqué, +2 si aucun but encaissé."""
    pts = goals_for
    if goals_for > goals_against:
        pts += 3
    elif goals_for == goals_against:
        pts += 1
    if goals_against == 0:
        pts += 2
    return pts


def fantasy_entry_points(picks, captain, team_points):
    """Total d'une équipe Fantasy. team_points : {teamId: points sur la journée}."""
    total = 0
    for t in picks:
        p = team_points.get(t, 0)
        total += p * 2 if t == captain else p
    return total


def team_price(position, table_size):
    """Prix d'un club selon son classement : 1er = 30, dernier = 10."""
    if not position or not table_size or table_size < 2:
        return PRICE_DEFAULT
    ratio = (table_size - position) / (table_size - 1)
    return int(round(PRICE_MIN + (PRICE_MAX - PRICE_MIN) * ratio))


def round_key(match):
    """Identifie la « journée » d'un match : numéro de journée, ou phase (ex. LAST_16) en coupe."""
    md = match.get("md")
    return f"md{md}" if md else (match.get("st") or "X")


def round_id(comp, season, key):
    return f"{comp}-{season}-{key}"


# ---------------------------------------------------------------------------
# Matchs
# ---------------------------------------------------------------------------
STATUS = {
    "SCHEDULED": "NS", "TIMED": "NS", "IN_PLAY": "LIVE", "LIVE": "LIVE", "PAUSED": "HT",
    "FINISHED": "FT", "POSTPONED": "PST", "SUSPENDED": "SUS", "CANCELLED": "CAN", "AWARDED": "AWD",
}
FINAL = {"FT", "AWD"}
VOID = {"CAN", "PST"}


def regular_time_score(score):
    """Score à la fin du temps réglementaire (prolongation et tirs au but exclus)."""
    if not score:
        return None, None
    rt = score.get("regularTime")
    ft = score.get("fullTime") or {}
    src = rt if rt and rt.get("home") is not None else ft
    return src.get("home"), src.get("away")


def parse_utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def compact_match(m):
    """Réduit un match football-data.org au strict nécessaire pour l'appli."""
    kick = parse_utc(m["utcDate"])
    hs, as_ = regular_time_score(m.get("score"))
    home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
    season = (m.get("season") or {}).get("startDate", "")[:4]
    return {
        "i": m["id"],
        "c": (m.get("competition") or {}).get("code", ""),
        "cn": (m.get("competition") or {}).get("name", ""),
        "k": int(kick.timestamp() * 1000),
        "s": STATUS.get(m.get("status", ""), "NS"),
        "h": home.get("shortName") or home.get("name") or "?",
        "a": away.get("shortName") or away.get("name") or "?",
        "hi": home.get("id") or 0,
        "ai": away.get("id") or 0,
        "hs": hs,
        "as": as_,
        "md": m.get("matchday"),
        "st": m.get("stage") or "",
        "se": int(season) if season.isdigit() else 0,
        **_extras(m),
    }


def _extras(m):
    """Champs facultatifs (absents si la source ne les donne pas, pour garder des fiches légères)."""
    out = {}
    ht = (m.get("score") or {}).get("halfTime") or {}
    if ht.get("home") is not None and ht.get("away") is not None:
        out["ht"] = [ht["home"], ht["away"]]
    minute = m.get("minute")
    if minute not in (None, ""):
        out["mn"] = str(minute)[:6]
    return out


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def week_key(d: date):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w}"


def board_ids(prefix, d: date, country):
    """Mêmes noms que l'appli (Boards.kt), précédés de « pred_ »."""
    keys = [f"d{d.isoformat()}", f"w{week_key(d)}", f"m{d.isoformat()[:7]}", "all"]
    scopes = ["world"]
    c = (country or "").strip().upper()
    if len(c) == 2 and c.isalpha():
        scopes.append(c)
    return [f"{prefix}{k}_{s}" for k in keys for s in scopes]


def fetch_windows(today: date):
    """Deux fenêtres de 10 jours max (limite de l'API) : de J-3 à J+13."""
    a = today - timedelta(days=3)
    return [(a, a + timedelta(days=9)), (a + timedelta(days=10), a + timedelta(days=16))]


# ---------------------------------------------------------------------------
# Modération
# ---------------------------------------------------------------------------
REPORTS_TO_HIDE = 3

# Liste volontairement courte : à compléter. Comparaison en minuscules, sur des mots entiers.
BANNED = [
    # fr
    "connard", "salope", "enculé", "pute", "nique ta mère",
    # en
    "fuck", "bitch", "cunt", "nigger", "faggot",
    # es
    "puta", "maricón", "hijo de puta",
    # pt
    "caralho", "viado", "filho da puta",
    # ar
    "شرموط", "منيوك", "كس أمك",
]

_BANNED_RE = [re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)") for w in BANNED]


def is_offensive(text):
    t = (text or "").lower()
    return any(r.search(t) for r in _BANNED_RE)


# ---------------------------------------------------------------------------
# Textes des notifications (5 langues)
# ---------------------------------------------------------------------------
MESSAGES = {
    "pred": {
        "en": ("Prediction result", "{h} – {a}: {rh}-{ra}. You earn {pts} pts!"),
        "fr": ("Résultat de ton pronostic", "{h} – {a} : {rh}-{ra}. Tu gagnes {pts} pts !"),
        "es": ("Resultado de tu pronóstico", "{h} – {a}: {rh}-{ra}. ¡Ganas {pts} pts!"),
        "pt": ("Resultado do seu palpite", "{h} – {a}: {rh}-{ra}. Você ganha {pts} pts!"),
        "ar": ("نتيجة توقعك", "{h} – {a}: {rh}-{ra}. ربحت {pts} نقطة!"),
    },
    "challenge_new": {
        "en": ("New challenge ⚔️", "{name} challenges you! Same questions, who will win?"),
        "fr": ("Nouveau défi ⚔️", "{name} te défie ! Mêmes questions, qui va gagner ?"),
        "es": ("Nuevo reto ⚔️", "¡{name} te reta! Mismas preguntas, ¿quién ganará?"),
        "pt": ("Novo desafio ⚔️", "{name} desafiou você! Mesmas perguntas, quem vai ganhar?"),
        "ar": ("تحدٍّ جديد ⚔️", "{name} يتحداك! الأسئلة نفسها، من سيفوز؟"),
    },
    "challenge_done": {
        "en": ("Challenge finished", "{name} answered your challenge: {mine} – {theirs}"),
        "fr": ("Défi terminé", "{name} a relevé ton défi : {mine} – {theirs}"),
        "es": ("Reto terminado", "{name} respondió a tu reto: {mine} – {theirs}"),
        "pt": ("Desafio concluído", "{name} respondeu ao seu desafio: {mine} – {theirs}"),
        "ar": ("انتهى التحدي", "{name} أجاب على تحديك: {mine} – {theirs}"),
    },
    "fantasy": {
        "en": ("Fantasy: round scored", "{comp}: your team scored {pts} pts"),
        "fr": ("Fantasy : journée terminée", "{comp} : ton équipe marque {pts} pts"),
        "es": ("Fantasy: jornada terminada", "{comp}: tu equipo suma {pts} pts"),
        "pt": ("Fantasy: rodada encerrada", "{comp}: seu time fez {pts} pts"),
        "ar": ("الفانتازي: انتهت الجولة", "{comp}: حصد فريقك {pts} نقطة"),
    },
}


def message(kind, lang, **kw):
    lang = lang if lang in ("en", "fr", "es", "pt", "ar") else "en"
    title, body = MESSAGES[kind][lang]
    return title, body.format(**kw)


# ---------------------------------------------------------------------------
# V4 : statistiques (équipes, effectifs, buteurs) et sources multiples
# ---------------------------------------------------------------------------
import unicodedata

POSITION_GROUPS = {
    "goalkeeper": "G",
    "defence": "D", "defender": "D", "centre-back": "D", "left-back": "D", "right-back": "D",
    "midfield": "M", "midfielder": "M", "central midfield": "M", "defensive midfield": "M",
    "attacking midfield": "M", "left midfield": "M", "right midfield": "M",
    "offence": "A", "attacker": "A", "forward": "A", "centre-forward": "A",
    "left winger": "A", "right winger": "A",
}


def position_group(pos):
    """G (gardien), D (défenseur), M (milieu), A (attaquant) ou "" si inconnu."""
    p = (pos or "").strip().lower()
    if p in POSITION_GROUPS:
        return POSITION_GROUPS[p]
    for key, g in (("keeper", "G"), ("back", "D"), ("defen", "D"), ("midfield", "M"),
                   ("wing", "A"), ("forward", "A"), ("striker", "A"), ("offen", "A")):
        if key in p:
            return g
    return ""


def compact_team(t, comp_code):
    """Fiche d'équipe (football-data.org /competitions/{code}/teams) : identité, entraîneur, effectif."""
    coach = t.get("coach") or {}
    squad = []
    for p in t.get("squad") or []:
        if not p.get("id") or not p.get("name"):
            continue
        squad.append({
            "i": p["id"], "n": p["name"][:40], "p": position_group(p.get("position")),
            "b": (p.get("dateOfBirth") or "")[:10], "nat": (p.get("nationality") or "")[:30],
            **({"no": p["shirtNumber"]} if isinstance(p.get("shirtNumber"), int) else {}),
        })
    return {
        "id": t["id"],
        "n": (t.get("name") or "?")[:60],
        "sn": (t.get("shortName") or t.get("name") or "?")[:30],
        "tla": (t.get("tla") or "")[:4],
        "f": t.get("founded") if isinstance(t.get("founded"), int) else None,
        "v": (t.get("venue") or "")[:60],
        "ad": (t.get("address") or "")[:80],
        "col": (t.get("clubColors") or "")[:40],
        "web": (t.get("website") or "")[:80],
        "coach": (coach.get("name") or "")[:40],
        "coachNat": (coach.get("nationality") or "")[:30],
        "area": ((t.get("area") or {}).get("name") or "")[:30],
        "comps": [comp_code],
        "sq": squad,
    }


def compact_scorer(s):
    """Une ligne du classement des buteurs (football-data.org /competitions/{code}/scorers)."""
    p, t = s.get("player") or {}, s.get("team") or {}
    return {
        "i": p.get("id") or 0, "n": (p.get("name") or "?")[:40], "nat": (p.get("nationality") or "")[:30],
        "t": (t.get("shortName") or t.get("name") or "?")[:30], "ti": t.get("id") or 0,
        "g": s.get("goals") or 0, "as": s.get("assists") or 0, "pen": s.get("penalties") or 0,
        "mp": s.get("playedMatches") or 0,
    }


def result_letter(goals_for, goals_against):
    return "W" if goals_for > goals_against else "L" if goals_for < goals_against else "D"


def recent_entries(m):
    """Pour un match terminé : la ligne « derniers résultats » de chaque équipe {teamId: {mid: ligne}}."""
    if m.get("s") not in FINAL or m.get("hs") is None or m.get("as") is None:
        return {}
    base = {"k": m["k"], "c": m["c"], "d": day_key(m["k"])}
    return {
        m["hi"]: {str(m["i"]): {**base, "o": m["a"], "oi": m["ai"], "h": True,
                                "gf": m["hs"], "ga": m["as"], "r": result_letter(m["hs"], m["as"])}},
        m["ai"]: {str(m["i"]): {**base, "o": m["h"], "oi": m["hi"], "h": False,
                                "gf": m["as"], "ga": m["hs"], "r": result_letter(m["as"], m["hs"])}},
    }


def season_year(today: date):
    """Saison en cours au format OpenLigaDB (2026 = saison 2026-2027, qui commence en juillet)."""
    return today.year if today.month >= 7 else today.year - 1


# ----- OpenLigaDB (Bundesliga : buteurs minute par minute, saisis en direct par la communauté) -----
OPENLIGA_LEAGUES = {"BL1": "bl1", "BL2": "bl2"}
OPENLIGA_ID_OFFSET = 900_000_000   # identifiants des matchs venant seulement d'OpenLigaDB (2. Bundesliga)

_STOP = {"fc", "sv", "vfb", "vfl", "tsg", "fsv", "sc", "sg", "bsc", "1", "04", "05", "07", "1846", "1899",
         "1860", "1848", "e", "v", "ev", "ac", "cf", "club", "de", "the"}


def _get(d, *keys, default=None):
    """OpenLigaDB écrit les champs en camelCase ou en PascalCase selon les versions."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def name_tokens(name):
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t.replace("'", ""))
    return {w for w in t.split() if w not in _STOP}


def name_similarity(a, b):
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    common = len(ta & tb)
    # « gladbach » dans « monchengladbach », « koln » dans « 1. fc koln »…
    partial = sum(1 for x in ta for y in tb if x != y and len(min(x, y, key=len)) >= 5 and (x in y or y in x))
    return (common + 0.8 * partial) / max(len(ta), len(tb))


def openliga_time_ms(om):
    iso = _get(om, "matchDateTimeUTC", "MatchDateTimeUTC")
    if not iso:
        return None
    return int(parse_utc(iso if ("Z" in iso or "+" in iso) else iso + "Z").timestamp() * 1000)


def openliga_events(om):
    """Buts d'un match OpenLigaDB : [[minute, côté (0 = domicile, 1 = extérieur), buteur, type]]."""
    goals = sorted(_get(om, "goals", "Goals", default=[]) or [],
                   key=lambda g: (_get(g, "matchMinute", "MatchMinute") or 0, _get(g, "goalID", "GoalID") or 0))
    events, prev = [], (0, 0)
    for g in goals:
        s1, s2 = _get(g, "scoreTeam1", "ScoreTeam1"), _get(g, "scoreTeam2", "ScoreTeam2")
        if s1 is None or s2 is None:
            continue
        side = 0 if s1 > prev[0] else 1 if s2 > prev[1] else None
        prev = (s1, s2)
        if side is None:
            continue
        kind = "og" if _get(g, "isOwnGoal", "IsOwnGoal") else "p" if _get(g, "isPenalty", "IsPenalty") else "g"
        minute = _get(g, "matchMinute", "MatchMinute")
        events.append([minute if isinstance(minute, int) else None, side,
                       (_get(g, "goalGetterName", "GoalGetterName") or "?")[:40], kind])
    return events


def openliga_score(om):
    """(buts domicile, buts extérieur, terminé ?) selon OpenLigaDB."""
    finished = bool(_get(om, "matchIsFinished", "MatchIsFinished", default=False))
    results = _get(om, "matchResults", "MatchResults", default=[]) or []
    final = [r for r in results if _get(r, "resultTypeID", "ResultTypeID") == 2]
    goals = _get(om, "goals", "Goals", default=[]) or []
    if final:
        r = final[0]
        return _get(r, "pointsTeam1", "PointsTeam1"), _get(r, "pointsTeam2", "PointsTeam2"), finished
    if goals:
        last = max(goals, key=lambda g: (_get(g, "scoreTeam1", "ScoreTeam1") or 0) + (_get(g, "scoreTeam2", "ScoreTeam2") or 0))
        return _get(last, "scoreTeam1", "ScoreTeam1"), _get(last, "scoreTeam2", "ScoreTeam2"), finished
    return None, None, finished


def openliga_team(om, n):
    t = _get(om, f"team{n}", f"Team{n}", default={}) or {}
    return {"id": _get(t, "teamId", "TeamId", default=0), "n": _get(t, "teamName", "TeamName", default="?"),
            "sn": _get(t, "shortName", "ShortName", default="")}


def match_openliga(fd_matches, ol_matches, max_gap_ms=2 * 3600 * 1000):
    """Associe les matchs football-data.org (nom long des équipes) aux matchs OpenLigaDB.

    fd_matches : [(id, coup d'envoi ms, nom domicile, nom extérieur)]. Renvoie {id fd: match OpenLigaDB}.
    """
    pairs = []
    for fid, k, hn, an in fd_matches:
        for om in ol_matches:
            ok = openliga_time_ms(om)
            if ok is None or abs(ok - k) > max_gap_ms:
                continue
            h, a = openliga_team(om, 1), openliga_team(om, 2)
            score = max(name_similarity(hn, h["n"]), name_similarity(hn, h["sn"])) + \
                max(name_similarity(an, a["n"]), name_similarity(an, a["sn"]))
            if score >= 0.6:
                pairs.append((score, fid, id(om), om))
    pairs.sort(key=lambda p: -p[0])
    used_fd, used_ol, out = set(), set(), {}
    for score, fid, oid, om in pairs:
        if fid in used_fd or oid in used_ol:
            continue
        used_fd.add(fid)
        used_ol.add(oid)
        out[fid] = om
    return out


def enrich_with_openliga(m, om, now_ms):
    """Ajoute les buteurs OpenLigaDB à un match et, s'il est en cours, son score plus récent."""
    m = dict(m)
    m["ol"] = _get(om, "matchID", "MatchID")
    ev = openliga_events(om)
    if ev:
        m["ev"] = ev
    h, a, finished = openliga_score(om)
    # Le score officiel (football-data) fait foi une fois le match terminé : il sert aux pronostics.
    if m["s"] not in FINAL and h is not None and a is not None and m["k"] <= now_ms and not finished:
        if (m["hs"] or 0) + (m["as"] or 0) <= h + a:
            m["hs"], m["as"] = h, a
        if m["s"] == "NS":
            m["s"] = "LIVE"
    return m


def compact_openliga_match(om, code, comp_name, now_ms):
    """Match qui n'existe que dans OpenLigaDB (2. Bundesliga), au même format que compact_match."""
    k = openliga_time_ms(om)
    mid = _get(om, "matchID", "MatchID")
    if k is None or mid is None:
        return None
    h, a = openliga_team(om, 1), openliga_team(om, 2)
    hs, as_, finished = openliga_score(om)
    if finished:
        status = "FT"
    elif now_ms - 3 * 3600 * 1000 < k <= now_ms:
        status = "LIVE"
    else:
        # Pas encore joué, ou résultat pas encore saisi : seul « matchIsFinished » vaut fin de match,
        # car les pronostics sont notés sur ce statut.
        status = "NS"
    if status == "LIVE" and hs is None:
        hs, as_ = 0, 0
    group = _get(om, "group", "Group", default={}) or {}
    season = _get(om, "leagueSeason", "LeagueSeason")
    out = {
        "i": OPENLIGA_ID_OFFSET + int(mid), "c": code, "cn": comp_name, "k": k, "s": status,
        "h": (h["sn"] or h["n"])[:30], "a": (a["sn"] or a["n"])[:30],
        "hi": OPENLIGA_ID_OFFSET + int(h["id"] or 0), "ai": OPENLIGA_ID_OFFSET + int(a["id"] or 0),
        "hs": hs, "as": as_, "md": _get(group, "groupOrderID", "GroupOrderID"), "st": "REGULAR_SEASON",
        "se": int(season) if isinstance(season, int) else 0, "ol": int(mid),
    }
    ev = openliga_events(om)
    if ev:
        out["ev"] = ev
    return out


def openliga_table(rows, id_offset=0):
    """Classement OpenLigaDB (getbltable) au format des classements de l'appli."""
    out = []
    for pos, r in enumerate(rows, start=1):
        out.append({
            "p": pos, "id": id_offset + int(_get(r, "teamInfoId", "TeamInfoId", default=0)),
            "t": _get(r, "shortName", "ShortName") or _get(r, "teamName", "TeamName", default="?"),
            "pl": _get(r, "matches", "Matches", default=0), "w": _get(r, "won", "Won", default=0),
            "d": _get(r, "draw", "Draw", default=0), "l": _get(r, "lost", "Lost", default=0),
            "gf": _get(r, "goals", "Goals", default=0), "ga": _get(r, "opponentGoals", "OpponentGoals", default=0),
            "gd": _get(r, "goalDiff", "GoalDiff", default=0), "pts": _get(r, "points", "Points", default=0),
        })
    return out


def openliga_scorers(rows, limit=30):
    """Buteurs OpenLigaDB (getgoalgetters) : nom et nombre de buts seulement."""
    rows = sorted(rows or [], key=lambda r: -(_get(r, "goalCount", "GoalCount", default=0) or 0))[:limit]
    return [{"i": 0, "n": (_get(r, "goalGetterName", "GoalGetterName") or "?")[:40], "nat": "", "t": "", "ti": 0,
             "g": _get(r, "goalCount", "GoalCount", default=0), "as": 0, "pen": 0, "mp": 0} for r in rows]


def needs_live_mode(matches, now_ms, horizon_ms=10 * 60 * 1000):
    """Vrai si un match est en cours ou commence dans les 10 minutes : on rafraîchit plus souvent."""
    for m in matches:
        if m["s"] in ("LIVE", "HT"):
            return True
        if m["s"] == "NS" and now_ms - 3 * 3600 * 1000 <= m["k"] <= now_ms + horizon_ms:
            return True
    return False
