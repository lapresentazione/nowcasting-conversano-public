import unittest

import csv
import io
from datetime import datetime, timezone

from alert_runner import (decide, DEFAULT, STALE_MS, append_csv, build_rows,
                          LOG_FIELDS, log_path)


def state():
    return dict(DEFAULT)


class RadarStateTest(unittest.TestCase):
    def test_first_alert(self):
        st, alert, save = decide(state(), 1, 300000)
        self.assertTrue(alert)
        self.assertTrue(save)
        self.assertTrue(st["active"])
        self.assertEqual(st["level"], 1)

    def test_duplicate_suppressed(self):
        active, _, _ = decide(state(), 1, 300000)
        same, alert, save = decide(active, 1, 600000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertEqual(same["last_scan"], 600000)
        self.assertEqual(same["level"], 1)

    def test_stronger_event_alert(self):
        active, _, _ = decide(state(), 1, 300000)
        stronger, alert, save = decide(active, 2, 600000)
        self.assertTrue(alert)
        self.assertTrue(save)
        self.assertEqual(stronger["level"], 2)

    def test_clear_after_two_distinct_scans(self):
        active, _, _ = decide(state(), 1, 300000)
        first_clear, alert, save = decide(active, 0, 600000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertTrue(first_clear["active"])
        same, alert, save = decide(first_clear, 0, 600000)
        self.assertFalse(save)
        self.assertEqual(same["quiet"], 1)
        second_clear, alert, save = decide(first_clear, 0, 900000)
        self.assertFalse(second_clear["active"])
        new_episode, alert, save = decide(second_clear, 1, 1200000)
        self.assertTrue(alert)

    def test_brief_clear_not_new_episode(self):
        active, _, _ = decide(state(), 1, 300000)
        one_clear, _, _ = decide(active, 0, 600000)
        wet_again, alert, save = decide(one_clear, 1, 900000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertEqual(wet_again["quiet"], 0)

    def test_no_writes_when_dry(self):
        s, alert, save = decide(state(), 0, 300000)
        self.assertFalse(alert)
        self.assertFalse(save)
        self.assertEqual(s, DEFAULT)

    def test_same_frame_twice_cannot_raise_new_event(self):
        active, _, _ = decide(state(), 1, 300000)
        again, alert, save = decide(active, 2, 300000)
        self.assertFalse(alert)
        self.assertFalse(save)
        self.assertEqual(again, active)

    def test_continuous_rain_is_not_notified_again(self):
        active, _, _ = decide(state(), 1, 300000)
        for timestamp in (600000, 900000, 1200000, 1500000):
            active, alert, save = decide(active, 1, timestamp)
            self.assertFalse(alert)
            self.assertTrue(save)
        self.assertEqual(active["last_scan"], 1500000)
        self.assertEqual(active["last_alert_scan"], 300000)

    def test_stale_episode_allows_new_alert(self):
        active, _, _ = decide(state(), 1, 300000)
        later = 300000 + STALE_MS + 300000
        st, alert, save = decide(active, 1, later)
        self.assertTrue(alert)
        self.assertTrue(save)
        self.assertEqual(st["last_alert_scan"], later)

    def test_stale_episode_closed_when_dry(self):
        active, _, _ = decide(state(), 2, 300000)
        later = 300000 + STALE_MS + 300000
        st, alert, save = decide(active, 0, later)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertFalse(st["active"])
        self.assertEqual(st["level"], 0)
        self.assertEqual(st["last_alert_scan"], 300000)

    def test_not_stale_within_limit(self):
        active, _, _ = decide(state(), 1, 300000)
        st, alert, _ = decide(active, 1, 300000 + STALE_MS)
        self.assertFalse(alert)
        self.assertTrue(st["active"])


def fake_obs(when, point=0.0):
    stats = {r: {"coverage": 1.0, "max_mm_h": 0.0, "cells_ge_1": 0,
                 "cells_ge_5": 0, "cells_ge_10": 0} for r in ("5", "10", "20", "40")}
    return {"when": when, "point": point, "stats": stats}


class RegistroTest(unittest.TestCase):
    RUN = datetime(2026, 10, 16, 9, 19, tzinfo=timezone.utc)

    def test_header_once_and_rows(self):
        base = {"run_utc": "2026-10-16T09:19:00Z", "esito": "ok", "livello": 0}
        t0 = int(self.RUN.timestamp() * 1000) - 15 * 60_000
        rows = build_rows(base, [fake_obs(t0 - 300000), fake_obs(t0, None)], self.RUN)
        text = append_csv("", rows)
        text = append_csv(text, rows)
        data = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(data), 4)
        self.assertEqual(text.count("run_utc,"), 1)
        self.assertEqual(data[1]["ruolo_scansione"], "ultima")
        self.assertEqual(data[1]["punto_mm_h"], "")
        self.assertEqual(data[1]["ritardo_scan_min"], "15.0")
        self.assertEqual(list(data[0].keys()), LOG_FIELDS)

    def test_error_row_without_scans(self):
        rows = build_rows({"esito": "errore_radar", "dettaglio": "ValueError"}, [], self.RUN)
        data = list(csv.DictReader(io.StringIO(append_csv("", rows))))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["esito"], "errore_radar")

    def test_daily_path(self):
        self.assertEqual(log_path(self.RUN), "registro/2026/2026-10-16.csv")


if __name__ == "__main__":
    unittest.main()
