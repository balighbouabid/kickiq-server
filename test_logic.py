"""Tests des règles du serveur. Lancer : python3 -m unittest server/test_logic.py"""
import unittest
from datetime import date

import logic


class PredictionTest(unittest.TestCase):
    def test_points(self):
        self.assertEqual(logic.prediction_points(2, 1, 2, 1), 10)   # exact
        self.assertEqual(logic.prediction_points(3, 2, 2, 1), 6)    # bon vainqueur + bon écart
        self.assertEqual(logic.prediction_points(1, 1, 2, 2), 6)    # nul avec le bon écart (0)
        self.assertEqual(logic.prediction_points(3, 0, 2, 1), 4)    # bon vainqueur seulement
        self.assertEqual(logic.prediction_points(0, 1, 2, 1), 0)    # mauvais vainqueur
        self.assertEqual(logic.prediction_points(1, 1, 1, 0), 0)    # nul pronostiqué, victoire réelle


class FantasyTest(unittest.TestCase):
    def test_team_points(self):
        self.assertEqual(logic.team_match_points(2, 0), 2 + 3 + 2)  # 2 buts, victoire, clean sheet
        self.assertEqual(logic.team_match_points(1, 1), 1 + 1)
        self.assertEqual(logic.team_match_points(0, 0), 0 + 1 + 2)
        self.assertEqual(logic.team_match_points(1, 3), 1)

    def test_entry_with_captain(self):
        pts = logic.fantasy_entry_points(["1", "2", "3", "4", "5"], "2", {"1": 5, "2": 7, "9": 100})
        self.assertEqual(pts, 5 + 14)

    def test_prices(self):
        self.assertEqual(logic.team_price(1, 20), 30)
        self.assertEqual(logic.team_price(20, 20), 10)
        self.assertEqual(logic.team_price(None, 20), 20)
        # 5 clubs les plus chers = 30+29+28+27+26 > 100 : impossible de prendre tout le top 5
        top5 = sum(logic.team_price(p, 20) for p in range(1, 6))
        self.assertGreater(top5, logic.FANTASY_BUDGET)

    def test_round_key(self):
        self.assertEqual(logic.round_key({"md": 11, "st": "REGULAR_SEASON"}), "md11")
        self.assertEqual(logic.round_key({"md": None, "st": "LAST_16"}), "LAST_16")


class MatchTest(unittest.TestCase):
    SAMPLE = {
        "id": 5001, "utcDate": "2026-10-31T19:45:00Z", "status": "FINISHED", "matchday": None, "stage": "FINAL",
        "competition": {"code": "CL", "name": "UEFA Champions League"},
        "season": {"startDate": "2026-08-01"},
        "homeTeam": {"id": 1, "name": "Arsenal FC", "shortName": "Arsenal"},
        "awayTeam": {"id": 2, "name": "Real Madrid CF", "shortName": "Real Madrid"},
        "score": {"duration": "PENALTY_SHOOTOUT", "fullTime": {"home": 6, "away": 5},
                  "regularTime": {"home": 1, "away": 1}, "extraTime": {"home": 0, "away": 0},
                  "penalties": {"home": 5, "away": 4}},
    }

    def test_compact_uses_regular_time(self):
        m = logic.compact_match(self.SAMPLE)
        self.assertEqual((m["hs"], m["as"]), (1, 1))
        self.assertEqual(m["s"], "FT")
        self.assertEqual(m["h"], "Arsenal")
        self.assertEqual(m["se"], 2026)
        self.assertEqual(logic.day_key(m["k"]), "2026-10-31")

    def test_board_ids_match_app(self):
        ids = logic.board_ids("pred_", date(2026, 10, 25), "fr")
        self.assertIn("pred_d2026-10-25_world", ids)
        self.assertIn("pred_w2026-W43_FR", ids)
        self.assertIn("pred_m2026-10_world", ids)
        self.assertIn("pred_all_FR", ids)
        self.assertEqual(len(logic.board_ids("pred_", date(2026, 10, 25), "")), 4)

    def test_windows_max_ten_days(self):
        for a, b in logic.fetch_windows(date(2026, 10, 30)):
            self.assertLessEqual((b - a).days, 9)


class ModerationTest(unittest.TestCase):
    def test_words(self):
        self.assertTrue(logic.is_offensive("quel connard cet arbitre"))
        self.assertFalse(logic.is_offensive("Quelle dispute pour la réputation du club !"))
        self.assertFalse(logic.is_offensive("Superbe match de Lille"))


class MessageTest(unittest.TestCase):
    def test_all_languages(self):
        for kind in logic.MESSAGES:
            for lang in ("en", "fr", "es", "pt", "ar", "xx"):
                title, body = logic.message(kind, lang, h="A", a="B", rh=1, ra=0, pts=10, name="X",
                                            mine=500, theirs=400, comp="PL")
                self.assertTrue(title and body)



class StatsV4Test(unittest.TestCase):
    def test_positions(self):
        self.assertEqual(logic.position_group("Goalkeeper"), "G")
        self.assertEqual(logic.position_group("Centre-Back"), "D")
        self.assertEqual(logic.position_group("Defensive Midfield"), "M")
        self.assertEqual(logic.position_group("Left Winger"), "A")
        self.assertEqual(logic.position_group(None), "")

    def test_compact_team(self):
        t = logic.compact_team({"id": 5, "name": "FC Bayern München", "shortName": "Bayern", "founded": 1900,
                                "coach": {"name": "V. Kompany"},
                                "squad": [{"id": 1, "name": "M. Neuer", "position": "Goalkeeper",
                                           "dateOfBirth": "1986-03-27", "nationality": "Germany"},
                                          {"name": "sans id"}]}, "BL1")
        self.assertEqual(t["sn"], "Bayern")
        self.assertEqual(t["comps"], ["BL1"])
        self.assertEqual(len(t["sq"]), 1)
        self.assertEqual(t["sq"][0]["p"], "G")

    def test_recent(self):
        m = {"i": 7, "c": "PL", "k": 0, "s": "FT", "h": "A", "a": "B", "hi": 1, "ai": 2, "hs": 2, "as": 1}
        r = logic.recent_entries(m)
        self.assertEqual(r[1]["7"]["r"], "W")
        self.assertEqual(r[2]["7"]["r"], "L")
        self.assertEqual(logic.recent_entries({**m, "s": "LIVE"}), {})

    def test_names(self):
        self.assertGreater(logic.name_similarity("Borussia Mönchengladbach", "Borussia Mönchengladbach"), 0.9)
        self.assertGreater(logic.name_similarity("1. FC Köln", "1. FC Köln"), 0.9)
        self.assertLess(logic.name_similarity("Borussia Dortmund", "Borussia Mönchengladbach"), 0.6)

    def test_openliga(self):
        om = {
            "matchID": 123, "matchDateTimeUTC": "2026-10-24T13:30:00Z", "matchIsFinished": False,
            "team1": {"teamId": 40, "teamName": "FC Bayern München", "shortName": "Bayern"},
            "team2": {"teamId": 7, "teamName": "Borussia Dortmund", "shortName": "BVB"},
            "matchResults": [],
            "goals": [
                {"goalID": 2, "scoreTeam1": 1, "scoreTeam2": 1, "matchMinute": 40, "goalGetterName": "Adeyemi"},
                {"goalID": 1, "scoreTeam1": 1, "scoreTeam2": 0, "matchMinute": 12, "goalGetterName": "Kane",
                 "isPenalty": True},
            ],
        }
        self.assertEqual(logic.openliga_events(om), [[12, 0, "Kane", "p"], [40, 1, "Adeyemi", "g"]])
        k = logic.openliga_time_ms(om)
        other = {**om, "matchID": 124, "team1": {"teamName": "VfB Stuttgart"}, "team2": {"teamName": "SC Freiburg"}}
        found = logic.match_openliga([(99, k, "FC Bayern München", "Borussia Dortmund"),
                                      (98, k, "VfB Stuttgart", "SC Freiburg")], [other, om])
        self.assertEqual(found[99]["matchID"], 123)
        self.assertEqual(found[98]["matchID"], 124)
        fd = {"i": 99, "k": k, "s": "NS", "hs": None, "as": None}
        live = logic.enrich_with_openliga(fd, om, k + 45 * 60 * 1000)
        self.assertEqual((live["s"], live["hs"], live["as"], live["ol"]), ("LIVE", 1, 1, 123))
        final = logic.enrich_with_openliga({**fd, "s": "FT", "hs": 2, "as": 1}, om, k + 3 * 3600 * 1000)
        self.assertEqual((final["hs"], final["as"]), (2, 1))   # le score officiel n'est jamais écrasé
        bl2 = logic.compact_openliga_match(om, "BL2", "2. Bundesliga", k + 600_000)
        self.assertEqual(bl2["i"], logic.OPENLIGA_ID_OFFSET + 123)
        self.assertEqual(bl2["s"], "LIVE")
        self.assertEqual(logic.compact_openliga_match({**om, "matchIsFinished": True}, "BL2", "x", k)["s"], "FT")

    def test_live_mode(self):
        self.assertTrue(logic.needs_live_mode([{"s": "LIVE", "k": 0}], 10))
        self.assertTrue(logic.needs_live_mode([{"s": "NS", "k": 5 * 60_000}], 0))
        self.assertFalse(logic.needs_live_mode([{"s": "NS", "k": 3600_000}], 0))
        self.assertEqual(logic.season_year(date(2026, 10, 30)), 2026)
        self.assertEqual(logic.season_year(date(2027, 3, 1)), 2026)


if __name__ == "__main__":
    unittest.main()
