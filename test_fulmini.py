"""Test dell'attività elettrica da satellite (fulmini.py) e della sua integrazione negli avvisi.

Le sequenze "reali" usano le tabelle a 5 minuti dei lampi entro 40 km stampate da
prova_fulmini.py v2 per il 30/10/2026 04:30-09:30 e per il 31/10/2026 22:00 - 01/11 04:00
(ora italiana). Ogni intervallo è ricostruito con i suoi conteggi per fascia (<20, 20-30,
30-40 km), mettendo i lampi a 15, 25 e 35 km a nord, a metà intervallo.
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

import fulmini as fl
from alert_runner import make_sender, is_lightning_alert, lightning_changed

ROMA = ZoneInfo("Europe/Rome")

TAB_30_10 = """05:30 0 0 1|05:35 0 2 2|05:40 0 0 1|05:45 0 0 1|05:50 0 0 2|06:05 1 0 1|06:10 0 0 4|
06:15 0 0 6|06:20 0 0 9|06:25 0 0 11|06:30 0 0 5|06:35 0 1 7|06:40 0 0 4|06:45 0 0 1|06:50 0 0 6|
06:55 0 0 8|07:00 0 0 5|07:05 0 0 4|07:10 0 0 5|07:15 0 1 3|07:20 0 0 5|07:25 0 0 5|07:30 0 0 3|
07:35 0 0 3|07:40 0 0 2|07:45 0 0 1|07:50 0 0 1|07:55 0 0 1|08:00 0 1 0|08:05 0 0 1|08:10 0 0 1|
08:25 0 0 2|08:35 1 0 2|08:45 0 1 3|08:50 0 1 3|08:55 0 2 1|09:00 0 0 3|09:05 0 1 1|09:10 0 2 2"""

TAB_31_10 = """22:15 0 0 2|22:25 0 0 3|22:30 0 0 4|22:35 0 0 4|22:40 0 0 4|22:45 0 0 3|22:50 0 0 3|
22:55 0 0 2|23:00 0 1 4|23:05 0 3 3|23:10 0 0 8|23:15 1 2 2|23:20 0 3 1|23:25 0 5 0|23:30 0 1 1|
23:35 0 0 3|23:40 0 1 1|23:45 2 0 1|23:50 1 0 0|23:55 0 2 0|+00:05 0 1 0|+00:10 0 2 0|+00:15 0 4 1|
+00:25 0 4 0|+00:30 0 1 0|+00:35 0 0 1|+00:40 0 1 1|+00:45 1 3 0|+00:50 2 0 1|+00:55 0 1 0|
+01:00 1 2 2|+01:05 2 0 3|+01:10 0 0 1|+01:15 0 0 4|+01:20 0 1 3|+01:25 0 1 3|+01:30 0 1 3|
+01:35 0 2 2|+01:40 0 1 3|+01:45 0 0 3|+01:50 0 0 2|+01:55 0 0 1|+03:05 0 1 0|+03:15 0 0 1|
+03:20 0 0 2"""

KM_PER_GRADO_LAT = 111.2


def lampi_da_tabella(tab, giorno):
    t, lat, lon = [], [], []
    for voce in tab.replace("\n", "").split("|"):
        ora, a, b, c = voce.split()
        g = giorno + timedelta(days=1) if ora.startswith("+") else giorno
        h, m = map(int, ora.lstrip("+").split(":"))
        centro = datetime(g.year, g.month, g.day, h, m, tzinfo=ROMA) + timedelta(minutes=2.5)
        for n, km in ((int(a), 15), (int(b), 25), (int(c), 35)):
            for k in range(n):
                t.append((centro + timedelta(seconds=k)).timestamp())
                lat.append(fl.LAT + km / KM_PER_GRADO_LAT)
                lon.append(fl.LON)
    return np.array(t), np.array(lat), np.array(lon)


def attivita_a(fine_locale, t, lat, lon, minuti=30):
    fine = fine_locale.astimezone(timezone.utc)
    k = (t <= fine.timestamp()) & (t > fine.timestamp() - minuti * 60)
    return fl.Attivita(fine, t[k], lat[k], lon[k])


def simula(tab, giorno, dalle, alle):
    t, lat, lon = lampi_da_tabella(tab, giorno)
    stato, avvisi = dict(fl.STATO_INIZIALE), []
    fine = dalle
    while fine <= alle:
        att = attivita_a(fine, t, lat, lon)
        stato, avviso = fl.valuta_avviso(stato, att)
        if avviso:
            avvisi.append(fine.strftime("%d/%m %H:%M"))
        fine += timedelta(minutes=10)
    return avvisi, stato


def att_sintetica(lampi, fine=datetime(2026, 10, 31, 22, 10, tzinfo=timezone.utc)):
    """lampi: [(minuti prima della fine, km, gradi)]."""
    t, lat, lon = [], [], []
    for eta, km, az in lampi:
        t.append(fine.timestamp() - eta * 60)
        lat.append(fl.LAT + km * np.cos(np.radians(az)) / KM_PER_GRADO_LAT)
        lon.append(fl.LON + km * np.sin(np.radians(az)) / (KM_PER_GRADO_LAT * np.cos(np.radians(fl.LAT))))
    return fl.Attivita(fine, t, lat, lon)


class DistanzaTest(unittest.TestCase):
    def test_nord_e_est(self):
        d, az = fl.distanza_direzione([fl.LAT + 0.1], [fl.LON])
        self.assertAlmostEqual(float(d[0]), 11.1, delta=0.2)
        self.assertEqual(fl.punto_cardinale(float(az[0])), "nord")
        d, az = fl.distanza_direzione([fl.LAT], [fl.LON + 0.1])
        self.assertAlmostEqual(float(d[0]), 8.4, delta=0.2)
        self.assertEqual(fl.punto_cardinale(float(az[0])), "est")

    def test_punto_reale_31_10(self):
        # flash del 31/10 23:17:51 stampato da prova_fulmini.py: 16,3 km N
        d, az = fl.distanza_direzione([41.118], [17.083])
        self.assertAlmostEqual(float(d[0]), 16.3, delta=0.2)
        self.assertEqual(fl.punto_cardinale(float(az[0])), "nord")


class AttivitaTest(unittest.TestCase):
    def test_riga_senza_lampi(self):
        att = att_sintetica([(5, 45, 0), (25, 10, 0)])  # uno oltre 40 km, uno troppo vecchio
        self.assertEqual(att.conta(40), 0)
        self.assertIn("nessun lampo entro 40 km", att.riga())

    def test_riga_con_lampi(self):
        att = att_sintetica([(3, 25, 90), (12, 16, 0), (8, 38, 315)])
        self.assertEqual(att.conta(40), 3)
        self.assertEqual(att.conta(30), 2)
        riga = att.riga()
        self.assertIn("3 lampi entro 40 km", riga)
        self.assertIn("16 km a nord", riga)
        self.assertIn("dati fino alle", riga)
        self.assertIn("vicino 16 km nord", att.registro())

    def test_non_disponibile(self):
        att = fl.Attivita.non_disponibile("tempo scaduto")
        self.assertIn("non disponibili (tempo scaduto)", att.riga())
        self.assertEqual(att.per_mappa(), [])
        stato = dict(fl.STATO_INIZIALE, lightning_active=True, lightning_last_inside=123)
        nuovo, avviso = fl.valuta_avviso(stato, att)
        self.assertFalse(avviso)
        self.assertEqual(nuovo, stato)

    def test_dati_vecchi(self):
        ora = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        dati = {"fine": ora - timedelta(minutes=50), "t": np.array([]), "lat": np.array([]),
                "lon": np.array([])}
        self.assertFalse(fl.costruisci(dati, ora).disponibile)
        dati["fine"] = ora - timedelta(minutes=8)
        self.assertTrue(fl.costruisci(dati, ora).disponibile)

    def test_mappa_ultimi_30_minuti(self):
        att = att_sintetica([(5, 20, 0), (25, 60, 90), (35, 20, 0)])
        self.assertEqual(len(att.per_mappa()), 2)


class RegolaATest(unittest.TestCase):
    def test_due_lampi_entro_30_km(self):
        nuovo, avviso = fl.valuta_avviso({}, att_sintetica([(3, 28, 0), (15, 22, 90)]))
        self.assertTrue(avviso)
        self.assertTrue(nuovo["lightning_active"])

    def test_un_solo_lampo_non_basta(self):
        self.assertFalse(fl.valuta_avviso({}, att_sintetica([(3, 12, 0)]))[1])

    def test_oltre_30_km_o_oltre_20_minuti_non_contano(self):
        self.assertFalse(fl.valuta_avviso({}, att_sintetica([(3, 31, 0), (4, 35, 0)]))[1])
        self.assertFalse(fl.valuta_avviso({}, att_sintetica([(3, 20, 0), (21, 20, 0)]))[1])

    def test_riarmo_dopo_60_minuti(self):
        fine0 = datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc)
        stato, avviso = fl.valuta_avviso({}, att_sintetica([(2, 20, 0), (5, 25, 0)], fine0))
        self.assertTrue(avviso)
        # 50 minuti dopo l'ultimo lampo vicino: ancora attivo, nessun nuovo avviso
        stato, avviso = fl.valuta_avviso(stato, att_sintetica([(1, 20, 0), (2, 21, 0)],
                                                              fine0 + timedelta(minutes=10)))
        self.assertFalse(avviso)
        stato, avviso = fl.valuta_avviso(stato, att_sintetica([], fine0 + timedelta(minutes=55)))
        self.assertTrue(stato["lightning_active"])
        stato, avviso = fl.valuta_avviso(stato, att_sintetica([], fine0 + timedelta(minutes=70)))
        self.assertFalse(stato["lightning_active"])
        self.assertFalse(avviso)
        stato, avviso = fl.valuta_avviso(stato, att_sintetica([(2, 20, 0), (5, 25, 0)],
                                                              fine0 + timedelta(minutes=80)))
        self.assertTrue(avviso)

    def test_sequenza_reale_31_10(self):
        avvisi, stato = simula(TAB_31_10, datetime(2026, 10, 31),
                               datetime(2026, 10, 31, 22, 10, tzinfo=ROMA),
                               datetime(2026, 11, 1, 4, 0, tzinfo=ROMA))
        # Un solo avviso, alle 23:10 (45 min prima della pioggia entro 5 km delle 23:55).
        # Il lampo isolato delle 03:05 arriva dopo il riarmo ma da solo non basta.
        self.assertEqual(avvisi, ["31/10 23:10"])
        self.assertFalse(stato["lightning_active"])

    def test_sequenza_reale_30_10(self):
        avvisi, _ = simula(TAB_30_10, datetime(2026, 10, 30),
                           datetime(2026, 10, 30, 4, 40, tzinfo=ROMA),
                           datetime(2026, 10, 30, 9, 30, tzinfo=ROMA))
        self.assertEqual(avvisi, ["30/10 05:40"])


class MessaggiTest(unittest.TestCase):
    RADAR = ("Radar Conversano: pioggia entro 5 km\nScansione: 31/10/2026 23:55 CET\n"
             "Massimo entro 5 km: 1.12 mm/h. Stima cella PGL320: 1.12 mm/h.\n"
             "Precipitazione stimata, non misura al suolo. Non implica un temporale. "
             "Avviso sperimentale non ufficiale.\n"
             "Radar Protezione Civile: https://mappe.protezionecivile.gov.it/it/mappe-rischi/piattaforma-radar/")

    def att(self):
        return att_sintetica([(3, 16.3, 0), (6, 21, 10), (9, 33, 45)])

    def test_testo_avviso(self):
        testo = fl.testo_avviso(self.att(), 1793487300000, "https://x")
        self.assertTrue(is_lightning_alert(testo))
        self.assertIn("2 lampi entro 30 km negli ultimi 20 minuti (3 entro 40 km)", testo)
        self.assertIn("non necessariamente fulmini a terra", testo)
        self.assertIn("\nScansione:", testo)
        self.assertLessEqual(len(testo), 1024)

    def test_unione_al_messaggio_radar(self):
        testo = fl.aggiungi_avviso_a_messaggio(self.RADAR, self.att())
        self.assertTrue(testo.startswith("Radar Conversano: pioggia entro 5 km\nAVVISO ATTIVITÀ ELETTRICA"))
        self.assertIn("\nScansione: 31/10/2026 23:55", testo)
        self.assertLessEqual(len(testo), 1024)


class FakeCtx:
    def __init__(self, png=b"png"):
        self.png, self.lampi = png, "non chiamato"

    def map_png(self, when, titolo, lampi=None):
        self.lampi = lampi
        return self.png, None


class SenderTest(unittest.TestCase):
    RADAR = MessaggiTest.RADAR

    def test_riga_e_lampi_sulla_mappa(self):
        foto, testi, ctx = [], [], FakeCtx()
        att = att_sintetica([(3, 25, 90)])
        send = make_sender(ctx, testi.append, lambda p, t: foto.append(t), log=lambda m: None, att=att)
        send(self.RADAR, 0)
        self.assertEqual(testi, [])
        self.assertIn("Attività elettrica (satellite, ultimi 20 min): 1 lampo entro 40 km", foto[0])
        self.assertEqual(len(ctx.lampi), 1)

    def test_avviso_unito_al_primo_messaggio(self):
        foto, ctx = [], FakeCtx()
        att = att_sintetica([(3, 16, 0), (6, 21, 10)])
        avviso = {"pendente": True, "inviato": False}
        send = make_sender(ctx, foto.append, lambda p, t: foto.append(t), log=lambda m: None,
                           att=att, avviso=avviso)
        send(self.RADAR, 0)
        send(self.RADAR, 0)
        self.assertTrue(avviso["inviato"])
        self.assertIn("AVVISO ATTIVITÀ ELETTRICA", foto[0])
        self.assertNotIn("AVVISO ATTIVITÀ ELETTRICA", foto[1])
        self.assertIn("Attività elettrica (satellite", foto[1])

    def test_avviso_autonomo_senza_riga_doppia(self):
        foto, ctx = [], FakeCtx()
        att = att_sintetica([(3, 16, 0), (6, 21, 10)])
        send = make_sender(ctx, foto.append, lambda p, t: foto.append(t), log=lambda m: None, att=att)
        send(fl.testo_avviso(att, 1793487300000, "https://x"), 0)
        self.assertNotIn("Attività elettrica (satellite", foto[0])

    def test_didascalia_troppo_lunga(self):
        foto, testi = [], []
        send = make_sender(FakeCtx(), testi.append, lambda p, t: foto.append(t), log=lambda m: None)
        lungo = "Titolo\nScansione: x\n" + "a" * 1100
        send(lungo, 0)
        self.assertEqual(foto, ["Titolo"])
        self.assertEqual(testi, [lungo])

    def test_senza_dati_satellitari_mappa_senza_lampi(self):
        foto, ctx = [], FakeCtx()
        att = fl.Attivita.non_disponibile("chiavi EUMETSAT assenti")
        send = make_sender(ctx, foto.append, lambda p, t: foto.append(t), log=lambda m: None, att=att)
        send(self.RADAR, 0)
        self.assertIsNone(ctx.lampi)
        self.assertIn("dati non disponibili (chiavi EUMETSAT assenti)", foto[0])

    def test_frase_temporale_tolta_solo_con_lampi(self):
        for lampi, attesa in (([(3, 25, 90)], False), ([], True), ([(3, 45, 90)], True)):
            foto = []
            send = make_sender(FakeCtx(), foto.append, lambda p, t: foto.append(t), log=lambda m: None,
                               att=att_sintetica(lampi))
            send(self.RADAR, 0)
            self.assertEqual("Non implica un temporale" in foto[0], attesa)
            self.assertIn("Precipitazione stimata, non misura al suolo. Avviso sperimentale", foto[0]
                          if not attesa else "Precipitazione stimata, non misura al suolo. Avviso sperimentale")
        foto = []
        send = make_sender(FakeCtx(), foto.append, lambda p, t: foto.append(t), log=lambda m: None,
                           att=fl.Attivita.non_disponibile("tempo scaduto"))
        send(self.RADAR, 0)
        self.assertIn("Non implica un temporale", foto[0])

    def test_stato_cambiato(self):
        self.assertFalse(lightning_changed({}, dict(fl.STATO_INIZIALE)))
        self.assertTrue(lightning_changed({}, dict(fl.STATO_INIZIALE, lightning_active=True)))


class ScaricamentoTest(unittest.TestCase):
    def test_errore_confinato(self):
        def guasto(ora):
            raise ConnectionError("https://segreto")
        ora = datetime.now(timezone.utc)
        att = fl.Scaricamento(ora, guasto).risultato(ora, tempo_max_s=5)
        self.assertFalse(att.disponibile)
        self.assertEqual(att.motivo, "errore ConnectionError")

    def test_tempo_scaduto(self):
        import time

        def lento(ora):
            time.sleep(3)
        ora = datetime.now(timezone.utc)
        att = fl.Scaricamento(ora, lento).risultato(ora, tempo_max_s=0.2)
        self.assertEqual(att.motivo, "tempo scaduto")

    def test_dati_buoni(self):
        ora = datetime.now(timezone.utc)

        def buono(o):
            return {"fine": o - timedelta(minutes=6), "t": np.array([o.timestamp() - 600]),
                    "lat": np.array([fl.LAT + 0.2]), "lon": np.array([fl.LON])}
        att = fl.Scaricamento(ora, buono).risultato(ora, tempo_max_s=5)
        self.assertTrue(att.disponibile)
        self.assertEqual(att.conta(40), 1)


class MappaLampiTest(unittest.TestCase):
    def test_mappa_reale_con_lampi(self):
        from test_alert_runner import ctx_from_fixture
        ctx, ultima = ctx_from_fixture()
        lampi = [(41.118, 17.083, 3.0), (41.2, 17.2, 15.0), (41.25, 16.85, 25.0), (42.5, 18.5, 5.0)]
        png, _ = ctx.map_png(ultima, "prova", lampi=lampi)
        self.assertTrue(png.startswith(b"\x89PNG"))
        png2, _ = ctx.map_png(ultima, "prova", lampi=[])
        self.assertTrue(png2.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
