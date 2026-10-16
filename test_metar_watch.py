import unittest
from datetime import datetime, timezone, timedelta

from metar_watch import (evaluate_all, build_message, decode_ts, describe,
                         group_obs, DEFAULT_STATE, EV_NEW_AT, EV_NEW_VC, EV_ARRIVED)

NOW = datetime(2026, 10, 16, 12, 5, tzinfo=timezone.utc)


def m(icao, minutes_ago, body, kind="METAR"):
    t = NOW - timedelta(minutes=minutes_ago)
    return f"{kind} {icao} {t:%d%H%M}Z {body}"


QUIET = "32008KT CAVOK 18/10 Q1018"


def history(icao, last_body, last_ago=10, kind="METAR"):
    """Osservazioni ogni 60 min per 4 ore, più l'ultima indicata."""
    lines = [m(icao, a, QUIET) for a in (250, 190, 130, 70)]
    lines.append(m(icao, last_ago, last_body, kind))
    return lines


def state():
    return dict(DEFAULT_STATE)


class MetarTest(unittest.TestCase):
    def ev(self, lines, st=None):
        return evaluate_all(lines, NOW, st or state())

    def test_quiet_no_event(self):
        lines = history("LIBV", QUIET) + history("LIBD", QUIET)
        _, events, statuses = self.ev(lines)
        self.assertEqual(events, {})
        self.assertEqual(statuses["LIBV"], "nessun nuovo evento")

    def test_new_ts_at_airport(self):
        lines = history("LIBV", "24015G30KT 3000 +TSRAGR FEW020CB 15/13 Q1012") + history("LIBD", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events["LIBV"][0], EV_NEW_AT)
        self.assertTrue(events["LIBV"][2])
        self.assertNotIn("LIBD", events)

    def test_new_vcts(self):
        lines = history("LIBD", "27010KT 9999 VCTS SCT030CB 19/12 Q1015") + history("LIBV", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events["LIBD"][0], EV_NEW_VC)

    def test_arrived_from_vicinity(self):
        lines = [m("LIBV", a, QUIET) for a in (250, 190, 130)]
        lines += [m("LIBV", 70, "27010KT 9999 VCTS SCT030CB 19/12 Q1015"),
                  m("LIBV", 10, "27015KT 4000 TSRA BKN025CB 16/14 Q1014")]
        lines += history("LIBD", QUIET)
        # La VCTS di 70 min fa è fuori dalla finestra "nuove" (65 min): resta l'arrivo.
        _, events, _ = self.ev(lines)
        self.assertEqual(events["LIBV"][0], EV_ARRIVED)

    def test_ongoing_not_notified(self):
        lines = [m("LIBV", a, QUIET) for a in (250, 190)]
        lines += [m("LIBV", 130, "TSRA 16/14 Q1014"), m("LIBV", 70, QUIET),
                  m("LIBV", 10, "-TSRA 16/14 Q1014")]
        lines += history("LIBD", QUIET)
        _, events, statuses = self.ev(lines)
        self.assertNotIn("LIBV", events)
        self.assertTrue(statuses["LIBV"].startswith("in corso"))

    def test_rets_only(self):
        lines = history("LIBV", "32008KT 9999 RETSRA 17/12 Q1016") + history("LIBD", QUIET)
        _, events, statuses = self.ev(lines)
        self.assertEqual(events, {})
        self.assertTrue(statuses["LIBV"].startswith("solo RETS"))

    def test_cb_without_ts_not_event(self):
        lines = history("LIBV", "32008KT 9999 FEW025CB SCT030TCU 17/12 Q1016") + history("LIBD", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events, {})

    def test_rmk_and_tempo_ignored(self):
        lines = history("LIBV", "32008KT CAVOK 17/12 Q1016 TEMPO TSRA RMK VCTS") + history("LIBD", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events, {})

    def test_stale_airport_not_verifiable(self):
        lines = [m("LIBV", a, QUIET) for a in (300, 240, 180, 120)] + history("LIBD", QUIET)
        _, events, statuses = self.ev(lines)
        self.assertEqual(statuses["LIBV"], "NON VERIFICABILE")
        self.assertEqual(events, {})

    def test_missing_history_possible_event(self):
        lines = [m("LIBV", 10, "TSRA 16/14 Q1014")] + history("LIBD", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events["LIBV"][0], EV_NEW_AT)
        self.assertFalse(events["LIBV"][2])

    def test_already_notified_is_not_repeated(self):
        lines = history("LIBV", "TSRA 16/14 Q1014") + history("LIBD", QUIET)
        _, events, _ = self.ev(lines)
        o = events["LIBV"][1]
        st = state()
        st["LIBV"] = int(o["ts"].timestamp() * 1000)
        _, events2, statuses2 = self.ev(lines, st)
        self.assertNotIn("LIBV", events2)
        self.assertEqual(statuses2["LIBV"], "in corso (già notificato)")

    def test_cor_replaces_original(self):
        lines = history("LIBV", QUIET)
        t = NOW - timedelta(minutes=10)
        lines.append(f"METAR COR LIBV {t:%d%H%M}Z 24010KT 5000 TSRA 16/14 Q1014")
        obs = group_obs(lines, NOW)["LIBV"]
        self.assertEqual(len(obs), 5)
        self.assertEqual(obs[-1]["st"], "AT")

    def test_speci_accepted(self):
        lines = history("LIBD", "22012KT 6000 TSRA FEW018CB 17/15 Q1013", last_ago=35, kind="SPECI")
        lines += history("LIBV", QUIET)
        _, events, _ = self.ev(lines)
        self.assertEqual(events["LIBD"][0], EV_NEW_AT)

    def test_decode(self):
        self.assertEqual(decode_ts("+TSRAGR"), "temporale forte con pioggia e grandine")
        self.assertEqual(decode_ts("-TSRA"), "temporale debole con pioggia")
        self.assertEqual(decode_ts("TS"), "temporale moderato, senza precipitazione segnalata")

    def test_message_contents(self):
        lines = history("LIBV", "24015G30KT 3000 +TSRAGR FEW020CB 15/13 Q1012 RMK WIND THR32 25018KT") + history("LIBD", QUIET)
        obs, events, statuses = self.ev(lines)
        text = build_message(events, obs, statuses)
        self.assertIn("Temporale a Gioia del Colle", text)
        self.assertIn("NUOVO TEMPORALE presso l'aeroporto", text)
        self.assertIn("temporale forte con pioggia e grandine", text)
        self.assertIn("da 240° (OSO), 28 km/h, raffiche 56 km/h", text)
        self.assertIn("Visibilità: 3000 m", text)
        self.assertIn("-3 °C rispetto al METAR", text)
        self.assertIn("Pressione QNH: 1012 hPa", text)
        self.assertIn("RMK WIND THR32 25018KT", text)
        self.assertIn("Bari Palese: ultimo METAR", text)
        self.assertIn("piattaforma-radar", text)


if __name__ == "__main__":
    unittest.main()
