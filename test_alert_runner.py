import unittest

import csv
import io
from datetime import datetime, timezone

from alert_runner import (decide, DEFAULT, STALE_MS, append_csv, build_rows,
                          LOG_FIELDS, log_path, plan_new_times, process_scans,
                          last_processed_in_log, PartialFailure, late_note)
from nowcasting import STEP


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


def wet_obs(when, ge1_5=0, ge5_10=0, ge10_10=0, point=0.5):
    o = fake_obs(when, point)
    o["stats"]["5"]["cells_ge_1"] = ge1_5
    o["stats"]["5"]["max_mm_h"] = 2.0 if ge1_5 else 0.0
    o["stats"]["10"]["cells_ge_5"] = ge5_10
    o["stats"]["10"]["cells_ge_10"] = ge10_10
    o["stats"]["10"]["max_mm_h"] = 12.0 if ge10_10 else 0.0
    return o


T0 = int(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def run_dt_after(when, minutes=10):
    return datetime.fromtimestamp((when + minutes * 60_000) / 1000, timezone.utc)


class PlanTest(unittest.TestCase):
    def test_nothing_new(self):
        self.assertEqual(plan_new_times(T0, T0), [])

    def test_two_new_in_order(self):
        self.assertEqual(plan_new_times(T0, T0 - 2 * STEP), [T0 - STEP, T0])

    def test_catch_up_is_capped_to_most_recent(self):
        times = plan_new_times(T0, 0, max_new=12)
        self.assertEqual(len(times), 12)
        self.assertEqual(times[-1], T0)
        self.assertEqual(times[0], T0 - 11 * STEP)

    def test_stale_limit_is_120_minutes(self):
        self.assertEqual(STALE_MS, 120 * 60_000)


class ProcessTest(unittest.TestCase):
    BASE = {"run_utc": "x", "dettaglio": "evento=workflow_dispatch"}

    def obs(self, levels):
        """levels: lista di (ora, osservazione) -> dict."""
        return dict(levels)

    def test_catch_up_alerts_l1_then_l2_in_order(self):
        times = [T0 + k * STEP for k in range(1, 5)]
        o = {T0: wet_obs(T0, ge1_5=3),
             times[0]: wet_obs(times[0], ge1_5=3),            # L1
             times[1]: wet_obs(times[1], ge1_5=3, ge5_10=4, ge10_10=1),
             times[2]: wet_obs(times[2], ge1_5=3, ge5_10=4, ge10_10=1),  # L2
             times[3]: wet_obs(times[3], ge1_5=3, ge5_10=4, ge10_10=1)}
        sent = []
        st, save, rows = process_scans(dict(DEFAULT), o, times,
                                       run_dt_after(times[-1]), self.BASE, sent.append)
        self.assertEqual(len(sent), 2)
        self.assertTrue(save)
        self.assertEqual(st["level"], 2)
        self.assertEqual(st["last_alert_scan"], times[2])
        self.assertEqual(st["last_scan"], times[3])
        self.assertEqual([r["notifica_inviata"] for r in rows], [1, 0, 1, 0])
        self.assertEqual([r["ruolo_scansione"] for r in rows],
                         ["intermedia"] * 3 + ["ultima"])

    def test_dry_scans_change_nothing(self):
        times = [T0 + STEP, T0 + 2 * STEP]
        o = {t: fake_obs(t) for t in [T0] + times}
        sent = []
        st, save, rows = process_scans(dict(DEFAULT), o, times,
                                       run_dt_after(times[-1]), self.BASE, sent.append)
        self.assertEqual(sent, [])
        self.assertFalse(save)
        self.assertEqual(st, DEFAULT)
        self.assertTrue(all(r["esito"] == "ok" for r in rows))

    def test_missing_scan_breaks_only_its_pairs(self):
        times = [T0 + k * STEP for k in range(1, 4)]
        o = {T0: wet_obs(T0, ge1_5=3), times[0]: None,
             times[1]: wet_obs(times[1], ge1_5=3), times[2]: wet_obs(times[2], ge1_5=3)}
        sent = []
        st, save, rows = process_scans(dict(DEFAULT), o, times,
                                       run_dt_after(times[-1]), self.BASE, sent.append)
        self.assertEqual([r["esito"] for r in rows],
                         ["dati_insufficienti", "dati_insufficienti", "ok"])
        self.assertEqual(rows[0]["motivo"], "scansione mancante")
        self.assertEqual(rows[0]["punto_mm_h"] if "punto_mm_h" in rows[0] else "", "")
        self.assertEqual(len(sent), 1)
        self.assertEqual(st["last_alert_scan"], times[2])

    def test_continuing_episode_not_renotified_across_runs(self):
        # Prima esecuzione: avviso L1.
        t1 = [T0 + STEP]
        o1 = {T0: wet_obs(T0, ge1_5=3), t1[0]: wet_obs(t1[0], ge1_5=3)}
        sent = []
        st, _, _ = process_scans(dict(DEFAULT), o1, t1, run_dt_after(t1[0]),
                                 self.BASE, sent.append)
        # Seconda esecuzione, 25 minuti dopo, con pioggia continua: nessun nuovo avviso.
        t2 = [T0 + k * STEP for k in range(2, 7)]
        o2 = {t: wet_obs(t, ge1_5=3) for t in [t1[0]] + t2}
        st, save, rows = process_scans(st, o2, t2, run_dt_after(t2[-1]),
                                       self.BASE, sent.append)
        self.assertEqual(len(sent), 1)
        self.assertTrue(save)
        self.assertEqual(st["last_scan"], t2[-1])

    def test_telegram_failure_keeps_earlier_alert(self):
        times = [T0 + k * STEP for k in range(1, 4)]
        o = {T0: wet_obs(T0, ge1_5=3),
             times[0]: wet_obs(times[0], ge1_5=3),
             times[1]: wet_obs(times[1], ge1_5=3, ge5_10=4, ge10_10=1),
             times[2]: wet_obs(times[2], ge1_5=3, ge5_10=4, ge10_10=1)}
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("rete")
        with self.assertRaises(PartialFailure) as ctx:
            process_scans(dict(DEFAULT), o, times, run_dt_after(times[-1]),
                          self.BASE, flaky)
        f = ctx.exception
        self.assertTrue(f.save)
        self.assertEqual(f.state["level"], 1)              # L1 già inviato e consolidato
        self.assertEqual(f.state["last_alert_scan"], times[0])
        self.assertEqual(f.rows[-1]["esito"], "errore_telegram")

    def test_late_note(self):
        dt = run_dt_after(T0, 35)
        self.assertIn("35 min dopo", late_note("x", T0, dt))
        self.assertEqual(late_note("x", T0, run_dt_after(T0, 12)), "x")


class LogReadTest(unittest.TestCase):
    def test_last_processed_old_and_new_formats(self):
        rows = [
            {"esito": "ok", "ruolo_scansione": "precedente", "scan_utc": "2026-10-30T12:20:00Z"},
            {"esito": "ok", "ruolo_scansione": "ultima", "scan_utc": "2026-10-30T12:15:00Z"},
            {"esito": "dati_insufficienti", "ruolo_scansione": "intermedia",
             "scan_utc": "2026-10-30T12:10:00Z"},
            {"esito": "nessuna_novita", "ruolo_scansione": "gia_elaborata",
             "scan_utc": "2026-10-30T12:30:00Z"},
            {"esito": "errore_radar", "ruolo_scansione": "", "scan_utc": ""},
        ]
        text = append_csv("", rows)
        expected = int(datetime(2026, 10, 30, 12, 15, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(last_processed_in_log(text), expected)

    def test_empty_log(self):
        self.assertEqual(last_processed_in_log(""), 0)

    def test_real_log_file_if_present(self):
        import os
        path = "registro/2026/2026-10-30.csv"
        if not os.path.exists(path):
            self.skipTest("registro reale non presente")
        with open(path, encoding="utf-8") as fh:
            self.assertGreater(last_processed_in_log(fh.read()), 0)


if __name__ == "__main__":
    unittest.main()
