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
                                       run_dt_after(times[-1]), self.BASE, lambda t, w: sent.append(t))
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
                                       run_dt_after(times[-1]), self.BASE, lambda t, w: sent.append(t))
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
                                       run_dt_after(times[-1]), self.BASE, lambda t, w: sent.append(t))
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
                                 self.BASE, lambda t, w: sent.append(t))
        # Seconda esecuzione, 25 minuti dopo, con pioggia continua: nessun nuovo avviso.
        t2 = [T0 + k * STEP for k in range(2, 7)]
        o2 = {t: wet_obs(t, ge1_5=3) for t in [t1[0]] + t2}
        st, save, rows = process_scans(st, o2, t2, run_dt_after(t2[-1]),
                                       self.BASE, lambda t, w: sent.append(t))
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

        def flaky(text, when=None):
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


# ------------------------------------------------- moto, avvicinamento, mappa
import numpy as np
import movimento as mv
from alert_runner import (approach_allowed, evaluate_approach, make_sender,
                          RadarContext, APPROACH_COOLDOWN_MS)

FIXTURE = "test_sri_2026-10-30_2220-2250.npz"  # scansioni reali 22:20-22:50 del 30/10/2026


def fixture():
    z = np.load(FIXTURE)
    campi = [a.astype(float) for a in z["campi"]]
    from rasterio.crs import CRS
    from affine import Affine
    geo = (CRS.from_wkt(str(z["crs_wkt"])), Affine(*z["transform"]), int(z["r0"]), int(z["c0"]))
    return campi, [int(t) for t in z["tempi"]], int(z["r_st"]), int(z["c_st"]), geo


def ctx_from_fixture(upto=None):
    campi, tempi, r, c, geo = fixture()
    n = len(campi) if upto is None else upto
    ctx = RadarContext(None, {t: b"" for t in tempi[:n]})
    ctx.crops = {t: (a, r, c, geo) for t, a in zip(tempi[:n], campi[:n])}
    return ctx, tempi[n - 1]


class MotoTest(unittest.TestCase):
    def test_synthetic_shift(self):
        from scipy import ndimage
        rng = np.random.default_rng(1)
        base = ndimage.gaussian_filter(rng.random((241, 241)), 6)
        base = np.where(base > np.quantile(base, .8), (base - base.min()) * 80, 0)
        moved = np.roll(np.roll(base, 3, axis=1), -2, axis=0)   # 3 km a est, 2 a nord
        dx, dy = mv.spostamento(base, moved)
        self.assertAlmostEqual(dx, 3, delta=0.2)
        self.assertAlmostEqual(dy, 2, delta=0.2)

    def test_no_rain_no_motion(self):
        z = np.zeros((241, 241))
        self.assertIsNone(mv.stima_moto([z] * 7))

    def test_real_motion_30_oct(self):
        campi, _, _, _, _ = fixture()
        m = mv.stima_moto(campi)
        self.assertEqual(len(m["vettori_kmh"]), 5)
        self.assertAlmostEqual(m["est_kmh"], -10.8, delta=1.5)    # verso ovest
        self.assertAlmostEqual(m["nord_kmh"], -1.4, delta=1.5)
        self.assertLess(m["dispersione_kmh"], 6)
        self.assertEqual(mv.punto_cardinale(m["provenienza_gradi"]), "est")

    def test_missing_frames_tolerated(self):
        campi, _, _, _, _ = fixture()
        campi[3] = None
        m = mv.stima_moto(campi)
        self.assertEqual(len(m["vettori_kmh"]), 3)

    def test_real_cell_ne_not_concordant_at_2250(self):
        campi, _, r, c, _ = fixture()
        m = mv.stima_moto(campi)
        cl = mv.celle(campi[-1], r, c, m)
        ne = cl[0]
        self.assertEqual(mv.punto_cardinale(ne["direzione_gradi"]), "nord-est")
        self.assertTrue(40 <= ne["arrivo_min"] <= 90)     # 22:50: arrivo stimato ~61 min
        self.assertLess(ne["quota_concordi"], 2 / 3)      # solo 3 vettori su 5 concordi
        self.assertIsNone(mv.cella_in_avvicinamento(cl))  # quindi nessun avviso

    def test_eta_is_entry_into_5_km_circle(self):
        # cella puntiforme 20 km a est che va verso ovest a 30 km/h sulla stazione:
        # entra nel cerchio di 5 km dopo 15 km, cioè 30 minuti
        est = np.array([20.0]); nord = np.array([0.0])
        self.assertAlmostEqual(mv._arrivo(est, nord, 20.0, -30.0, 0.0), 30.0, places=6)

    def test_approach_rule_thresholds(self):
        base = {"arrivo_min": 30.0, "arrivo_range_min": (25, 40), "quota_concordi": 0.8}
        self.assertIsNotNone(mv.cella_in_avvicinamento([base]))
        self.assertIsNone(mv.cella_in_avvicinamento([dict(base, arrivo_min=61.0)]))
        self.assertIsNone(mv.cella_in_avvicinamento([dict(base, arrivo_min=3.0)]))
        self.assertIsNone(mv.cella_in_avvicinamento([dict(base, quota_concordi=0.6)]))


class AvvicinamentoTest(unittest.TestCase):
    def test_allowed_rules(self):
        s = dict(DEFAULT)
        self.assertTrue(approach_allowed(s, T0))
        self.assertFalse(approach_allowed(dict(s, active=True), T0))
        s2 = dict(s, approach_scan=T0)
        self.assertFalse(approach_allowed(s2, T0 + 60 * 60_000))
        self.assertTrue(approach_allowed(s2, T0 + APPROACH_COOLDOWN_MS + STEP))
        self.assertTrue(approach_allowed(dict(s2, last_alert_scan=T0 + STEP), T0 + 2 * STEP))

    def test_evaluate_sends_once(self):
        ctx, latest = ctx_from_fixture()
        sent = []
        cella = {"arrivo_min": 30.0, "arrivo_range_min": (25.0, 40.0), "quota_concordi": 0.8,
                 "dist_km": 9.0, "direzione_gradi": 45.0, "max_mm_h": 3.2, "area_km2": 20,
                 "punto_km": (6.0, 6.0)}
        orig = mv.cella_in_avvicinamento
        mv.cella_in_avvicinamento = lambda cl: cella
        try:
            st, save, info = evaluate_approach(ctx, dict(DEFAULT), latest, run_dt_after(latest),
                                               lambda t, w: sent.append(t))
            self.assertTrue(save)
            self.assertEqual(st["approach_scan"], latest)
            self.assertIn("avviso_avvicinamento=1", info)
            self.assertIn("pioggia in avvicinamento", sent[0])
            self.assertIn("Arrivo stimato fra circa 30 min", sent[0])
            st2, save2, info2 = evaluate_approach(ctx, st, latest, run_dt_after(latest),
                                                  lambda t, w: sent.append(t))
            self.assertFalse(save2)
            self.assertEqual(len(sent), 1)
            self.assertIn("avviso_avvicinamento=0", info2)
        finally:
            mv.cella_in_avvicinamento = orig

    def test_real_case_no_approach_at_2250(self):
        ctx, latest = ctx_from_fixture()
        sent = []
        st, save, info = evaluate_approach(ctx, dict(DEFAULT), latest, run_dt_after(latest),
                                           lambda t, w: sent.append(t))
        self.assertEqual(sent, [])
        self.assertFalse(save)
        self.assertIn("in moto da est", info)


class MappaInvioTest(unittest.TestCase):
    def test_map_png_real(self):
        ctx, latest = ctx_from_fixture()
        png, moto = ctx.map_png(latest, "Prova")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIsNotNone(moto)

    def test_sender_photo_with_motion(self):
        ctx, latest = ctx_from_fixture()
        photos, texts = [], []
        send = make_sender(ctx, texts.append, lambda png, t: photos.append(t), log=lambda m: None)
        send("Radar Conversano: pioggia entro 5 km\nScansione: x\nAltro", latest)
        self.assertEqual(texts, [])
        self.assertIn("Pioggia in moto da est", photos[0])

    def test_sender_falls_back_to_text(self):
        ctx, latest = ctx_from_fixture()
        texts = []

        def broken(png, text):
            raise RuntimeError("rete")
        send = make_sender(ctx, texts.append, broken, log=lambda m: None)
        send("Radar Conversano: pioggia entro 5 km\nScansione: x", latest)
        self.assertEqual(len(texts), 1)

    def test_sender_without_scan_sends_text(self):
        ctx = RadarContext(None, {})
        texts = []
        send = make_sender(ctx, texts.append, lambda p, t: None, log=lambda m: None)
        send("testo", T0)
        self.assertEqual(texts, ["testo"])
