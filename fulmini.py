"""Attività elettrica dal satellite (EUMETSAT MTG Lightning Imager) per la PGL320.

Sperimentale, NON allerta ufficiale. Usato da alert_runner.py (versione 5, 01/11/2026).

Fonte: prodotto LI-2-LFL (lampi, disco intero, pacchetti da 10 minuti pubblicati circa
40 secondi dopo la fine della finestra), collezione EO:EUM:DAT:0691, licenza CC BY 4.0.
Il satellite rileva i lampi totali, comprese le scariche tra nube e nube: i messaggi
parlano di "attività elettrica", non di fulmini a terra.

Scelte di Michele (01/11/2026):
- riga sull'attività elettrica in ogni messaggio radar e lampi sulla mappa;
- avviso autonomo con la regola A: almeno MIN_LAMPI lampi entro SOGLIA_KM negli ultimi
  FINESTRA_MIN minuti; un nuovo avviso solo dopo RIARMO_MIN minuti senza lampi entro
  SOGLIA_KM; sonoro anche di notte.

Ogni errore resta confinato qui: senza dati satellitari il sistema radar lavora come prima.
Le chiavi EUMETSAT_CONSUMER_KEY e EUMETSAT_CONSUMER_SECRET non vengono mai stampate.
"""
import io
import math
import os
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

LAT, LON = 40.976, 17.13
ITALIA = ZoneInfo("Europe/Rome")
COLLEZIONE = "EO:EUM:DAT:0691"
EPOCA = datetime(2000, 1, 1, tzinfo=timezone.utc)

RAGGIO_RIGA_KM = 40        # riga nei messaggi radar
SOGLIA_KM = 30             # regola A
MIN_LAMPI = 2              # regola A
FINESTRA_MIN = 20          # regola A e riga nei messaggi
RIARMO_MIN = 60            # nuovo avviso solo dopo 60 min senza lampi entro SOGLIA_KM
MAPPA_MIN = 30             # lampi disegnati sulla mappa
RAGGIO_TENUTI_KM = 75      # lampi conservati (la mappa copre +-50 km, cioè fino a 71 km)
PACCHETTI = 3              # 3 pacchetti da 10 minuti = 30 minuti
DATI_VECCHI_MIN = 35       # oltre, i dati satellitari sono considerati non disponibili
TEMPO_MAX_S = 90           # tempo massimo per scaricare i lampi
NOTA_SATELLITE = ("Lampi rilevati dal satellite EUMETSAT, comprese le scariche tra nube e nube: "
                  "indicano attività elettrica a quella distanza, non necessariamente fulmini a terra.")

PUNTI = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest"]


def punto_cardinale(gradi):
    return PUNTI[int(((gradi % 360) + 22.5) // 45) % 8]


def distanza_direzione(lat, lon):
    """Distanza (km) e azimut (gradi) dalla stazione (formula dell'emisenoverso)."""
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    r = 6371.0
    p1, p2 = math.radians(LAT), np.radians(lat)
    dl = np.radians(lon) - math.radians(LON)
    a = np.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    d = 2 * r * np.arcsin(np.sqrt(a))
    y = np.sin(dl) * np.cos(p2)
    x = math.cos(p1) * np.sin(p2) - math.sin(p1) * np.cos(p2) * np.cos(dl)
    return d, (np.degrees(np.arctan2(y, x)) + 360) % 360


# ------------------------------------------------------------------ lettura

def _trova(ds, nome):
    if nome in ds.variables:
        return ds.variables[nome]
    for g in ds.groups.values():
        v = _trova(g, nome)
        if v is not None:
            return v
    return None


def leggi_lampi_nc(contenuto):
    """Bytes di un file netCDF LFL -> (tempi UTC in secondi Unix, lat, lon) come array."""
    import netCDF4
    with netCDF4.Dataset("lfl.nc", memory=contenuto) as ds:
        lat, lon, t = _trova(ds, "latitude"), _trova(ds, "longitude"), _trova(ds, "flash_time")
        if lat is None or lon is None or t is None:
            raise ValueError("variabili latitude/longitude/flash_time assenti")
        lat = np.ma.filled(lat[:].astype(float), np.nan)
        lon = np.ma.filled(lon[:].astype(float), np.nan)
        sec = np.ma.filled(t[:].astype(float), np.nan)
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(sec)
    return sec[ok] + EPOCA.timestamp(), lat[ok], lon[ok]


def _file_nc(prodotto):
    voci = [e for e in prodotto.entries if e.endswith(".nc") and "BODY" in e]
    if voci:
        out = []
        for e in voci:
            with prodotto.open(entry=e) as f:
                out.append(f.read())
        return out
    with prodotto.open() as f:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
    return [z.read(n) for n in z.namelist() if n.endswith(".nc") and "BODY" in n]


def scarica(ora_utc, pacchetti=PACCHETTI):
    """Ultimi pacchetti LFL disponibili -> dati per costruisci().

    Restituisce {"fine": datetime UTC della fine dell'ultima finestra, "pacchetti": n,
    "t": secondi Unix, "lat": ..., "lon": ...} con i soli lampi entro RAGGIO_TENUTI_KM.
    """
    import eumdac
    chiave = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    segreto = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if not chiave or not segreto:
        raise RuntimeError("chiavi EUMETSAT assenti")
    token = eumdac.AccessToken((chiave, segreto))
    coll = eumdac.DataStore(token).get_collection(COLLEZIONE)
    dal = ora_utc - timedelta(minutes=10 * pacchetti + 15)
    prodotti = sorted(coll.search(dtstart=dal, dtend=ora_utc),
                      key=lambda p: p.sensing_end)[-pacchetti:]
    if not prodotti:
        raise RuntimeError("nessun pacchetto LFL recente")
    tt, la, lo = [], [], []
    for p in prodotti:
        for contenuto in _file_nc(p):
            t, lat, lon = leggi_lampi_nc(contenuto)
            d, _ = distanza_direzione(lat, lon)
            k = d < RAGGIO_TENUTI_KM
            tt.append(t[k]); la.append(lat[k]); lo.append(lon[k])
    fine = prodotti[-1].sensing_end.replace(tzinfo=timezone.utc)
    return {"fine": fine, "pacchetti": len(prodotti),
            "t": np.concatenate(tt) if tt else np.array([]),
            "lat": np.concatenate(la) if la else np.array([]),
            "lon": np.concatenate(lo) if lo else np.array([])}


class Scaricamento:
    """Scarica i lampi in parallelo al radar, con un tempo massimo (mai bloccante)."""

    def __init__(self, ora_utc, funzione=scarica):
        self.inizio = datetime.now(timezone.utc)
        self.esito, self.errore = None, None
        self._thread = threading.Thread(target=self._lavora, args=(funzione, ora_utc), daemon=True)
        self._thread.start()

    def _lavora(self, funzione, ora_utc):
        try:
            self.esito = funzione(ora_utc)
        except Exception as error:  # nessun dettaglio: potrebbe contenere URL o chiavi
            self.errore = type(error).__name__

    def risultato(self, ora_utc, tempo_max_s=TEMPO_MAX_S):
        """Attività elettrica pronta per l'uso (Attivita), anche se non disponibile."""
        trascorso = (datetime.now(timezone.utc) - self.inizio).total_seconds()
        self._thread.join(max(0.0, tempo_max_s - trascorso))
        if self._thread.is_alive():
            return Attivita.non_disponibile("tempo scaduto")
        if self.errore:
            return Attivita.non_disponibile(f"errore {self.errore}")
        return costruisci(self.esito, ora_utc)


# ------------------------------------------------------------- elaborazione

class Attivita:
    """Lampi vicini alla stazione con distanza, direzione ed età rispetto alla fine dei dati."""

    def __init__(self, fine=None, t=(), lat=(), lon=(), motivo=""):
        self.disponibile = fine is not None
        self.motivo = motivo
        self.fine = fine
        self.t = np.asarray(t, dtype=float)
        self.lat = np.asarray(lat, dtype=float)
        self.lon = np.asarray(lon, dtype=float)
        if len(self.t):
            self.d, self.az = distanza_direzione(self.lat, self.lon)
        else:
            self.d, self.az = np.array([]), np.array([])
        self.eta_min = ((fine.timestamp() - self.t) / 60.0) if fine is not None else np.array([])

    @classmethod
    def non_disponibile(cls, motivo):
        return cls(None, motivo=motivo)

    def _scelta(self, raggio_km, minuti):
        if not self.disponibile or not len(self.t):
            return np.array([], dtype=int)
        k = (self.d < raggio_km) & (self.eta_min >= 0) & (self.eta_min < minuti)
        return np.where(k)[0]

    def conta(self, raggio_km, minuti=FINESTRA_MIN):
        return len(self._scelta(raggio_km, minuti))

    def piu_vicino(self, raggio_km, minuti=FINESTRA_MIN):
        idx = self._scelta(raggio_km, minuti)
        if not len(idx):
            return None
        i = idx[int(np.argmin(self.d[idx]))]
        ora = datetime.fromtimestamp(self.t[i], ITALIA).strftime("%H:%M")
        return {"dist_km": float(self.d[i]), "direzione": punto_cardinale(self.az[i]),
                "ora": ora, "t": float(self.t[i])}

    def ultimo_entro(self, raggio_km, minuti=FINESTRA_MIN):
        idx = self._scelta(raggio_km, minuti)
        return float(self.t[idx].max()) if len(idx) else None

    def per_mappa(self, minuti=MAPPA_MIN):
        """[(lat, lon, età in minuti)] dei lampi degli ultimi `minuti`."""
        idx = self._scelta(1e9, minuti)
        return [(float(self.lat[i]), float(self.lon[i]), float(self.eta_min[i])) for i in idx]

    def ora_fine(self):
        return self.fine.astimezone(ITALIA).strftime("%H:%M") if self.fine else ""

    def riga(self):
        """Riga per i messaggi radar."""
        if not self.disponibile:
            return f"Attività elettrica (satellite): dati non disponibili ({self.motivo})."
        n = self.conta(RAGGIO_RIGA_KM)
        if n == 0:
            return (f"Attività elettrica (satellite, ultimi {FINESTRA_MIN} min): "
                    f"nessun lampo entro {RAGGIO_RIGA_KM} km (dati fino alle {self.ora_fine()}).")
        v = self.piu_vicino(RAGGIO_RIGA_KM)
        lampi = "lampo" if n == 1 else "lampi"
        return (f"Attività elettrica (satellite, ultimi {FINESTRA_MIN} min): {n} {lampi} entro "
                f"{RAGGIO_RIGA_KM} km, il più vicino a {v['dist_km']:.0f} km a {v['direzione']} "
                f"alle {v['ora']} (dati fino alle {self.ora_fine()}).")

    def registro(self):
        """Testo per la colonna dettaglio del registro."""
        if not self.disponibile:
            return f"fulmini=non disponibili ({self.motivo})"
        v = self.piu_vicino(RAGGIO_RIGA_KM)
        testo = (f"fulmini={self.conta(RAGGIO_RIGA_KM)} entro {RAGGIO_RIGA_KM} km, "
                 f"{self.conta(SOGLIA_KM)} entro {SOGLIA_KM} km in {FINESTRA_MIN} min")
        if v:
            testo += f", vicino {v['dist_km']:.0f} km {v['direzione']} {v['ora']}"
        return testo + f", dati fino {self.ora_fine()}"


def costruisci(dati, ora_utc):
    """Dati di scarica() -> Attivita; dati troppo vecchi = non disponibili."""
    if not dati:
        return Attivita.non_disponibile("nessun dato")
    fine = dati["fine"]
    ritardo = (ora_utc - fine).total_seconds() / 60.0
    if ritardo > DATI_VECCHI_MIN:
        return Attivita.non_disponibile(f"ultimi dati di {ritardo:.0f} min fa")
    return Attivita(fine, dati["t"], dati["lat"], dati["lon"])


# ------------------------------------------------------------------ avviso

STATO_INIZIALE = {"lightning_active": False, "lightning_last_inside": 0, "lightning_alert": 0}


def valuta_avviso(stato, att):
    """Regola A con riarmo. Restituisce (campi di stato aggiornati, avviso?).

    stato: dizionario con i campi di STATO_INIZIALE (gli assenti valgono il default).
    lightning_last_inside: ora (ms) dell'ultimo lampo visto entro SOGLIA_KM;
    lightning_active: True dopo un avviso, finché non passano RIARMO_MIN minuti senza
    lampi entro SOGLIA_KM; lightning_alert: ora (ms) della fine dei dati dell'ultimo avviso.
    Con dati non disponibili lo stato non cambia e non parte alcun avviso.
    """
    nuovo = {k: stato.get(k, v) for k, v in STATO_INIZIALE.items()}
    if not att.disponibile:
        return nuovo, False
    fine_ms = int(att.fine.timestamp() * 1000)
    ultimo = att.ultimo_entro(SOGLIA_KM)
    if ultimo is not None:
        nuovo["lightning_last_inside"] = max(int(nuovo["lightning_last_inside"]), int(ultimo * 1000))
    if nuovo["lightning_active"]:
        if fine_ms - int(nuovo["lightning_last_inside"]) >= RIARMO_MIN * 60_000:
            nuovo["lightning_active"] = False
        return nuovo, False
    if att.conta(SOGLIA_KM) >= MIN_LAMPI:
        nuovo["lightning_active"] = True
        nuovo["lightning_alert"] = fine_ms
        return nuovo, True
    return nuovo, False


def blocco_avviso(att):
    """Righe dell'avviso di attività elettrica (senza intestazione)."""
    n30, n40 = att.conta(SOGLIA_KM), att.conta(RAGGIO_RIGA_KM)
    v = att.piu_vicino(SOGLIA_KM)
    return (f"{n30} lampi entro {SOGLIA_KM} km negli ultimi {FINESTRA_MIN} minuti "
            f"({n40} entro {RAGGIO_RIGA_KM} km); il più vicino a {v['dist_km']:.0f} km "
            f"a {v['direzione']} alle {v['ora']}. Dati satellitari fino alle {att.ora_fine()}.\n"
            f"{NOTA_SATELLITE}")


def testo_avviso(att, ora_scansione_ms, link):
    """Avviso autonomo di attività elettrica (la mappa radar è quella di ora_scansione_ms)."""
    t = datetime.fromtimestamp(ora_scansione_ms / 1000, ITALIA).strftime("%d/%m/%Y %H:%M %Z")
    return (f"Radar Conversano: attività elettrica entro {SOGLIA_KM} km\n"
            f"{blocco_avviso(att)}\n"
            f"Scansione: {t} (mappa radar)\n"
            "Avviso non ufficiale.\n"
            f"Radar Protezione Civile: {link}")


def aggiungi_avviso_a_messaggio(testo, att):
    """Unisce l'avviso di attività elettrica a un messaggio radar della stessa esecuzione."""
    return testo.replace("\nScansione:", f"\nAVVISO ATTIVITÀ ELETTRICA entro {SOGLIA_KM} km: "
                         f"{blocco_avviso(att)}\nScansione:", 1)
