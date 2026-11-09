"""Avvisi radar per PGL320 con stato persistito nei file del repository.

L'uso --dry-run non invia messaggi né modifica stato o registro.
L'uso --notify richiede GITHUB_TOKEN e i segreti Telegram.

Versione 2 (registro esecuzioni):
- a ogni esecuzione aggiunge righe a registro/AAAA/AAAA-MM-GG.csv, anche in
  caso di errore o di dati insufficienti (segnale di vita + base per la
  calibrazione delle soglie);
- un episodio attivo senza scansioni valide elaborate da oltre STALE_MS viene
  chiuso d'ufficio, così una pioggia successiva produce di nuovo un avviso.
Il formato di state/stato_avvisi.json non cambia.

Versione 3 (esecuzioni regolari e recupero, 30/10/2026):
- ogni esecuzione elabora tutte le scansioni SRI pubblicate dopo l'ultima già
  elaborata (stato + registro), fino a MAX_NEW_SCANS, in ordine cronologico:
  un'esecuzione saltata o ritardata non fa più perdere scansioni;
- pensato per l'avvio esterno regolare (workflow_dispatch da Google Apps
  Script, vedi apps_script/RadarDispatch.gs); il cron resta come riserva;
- STALE_MS portato a 120 minuti (tampone contro i ritardi di GitHub);
- una riga di registro per ogni scansione nuova (ruolo "ultima" o
  "intermedia"); se non c'è nulla di nuovo, una riga "gia_elaborata" con
  esito "nessuna_novita" (segnale di vita). La colonna dettaglio riporta il
  tipo di avvio (evento=schedule / workflow_dispatch).

Versione 4 (moto delle celle, avvicinamento e mappa, 30/10/2026):
- se c'è pioggia entro 60 km, stima il moto delle celle sulle scansioni degli
  ultimi 30 minuti (movimento.py) e invia un avviso di AVVICINAMENTO quando una
  cella di almeno 0,5 mm/h e 4 km² è stimata in arrivo entro 5-60 minuti con
  almeno due terzi delle stime concordi; un solo avviso per episodio (vedi
  approach_allowed); nessun avviso di avvicinamento durante un episodio L1/L2;
- a ogni avviso (avvicinamento, L1, L2) allega una mappa di +-50 km (mappa.py);
  se la mappa o l'invio della foto falliscono, invia il solo testo;
- registra nel campo dettaglio della riga "ultima" il moto stimato e la cella in
  avvicinamento, per le verifiche successive;
- stato: nuovo campo facoltativo "approach_scan" (ora della scansione dell'ultimo
  avviso di avvicinamento); gli altri campi non cambiano.

Versione 5 (attività elettrica dal satellite, 01/11/2026, scelte di Michele):
- a ogni esecuzione scarica, in parallelo al radar e con un tempo massimo, i lampi
  degli ultimi 30 minuti del satellite EUMETSAT MTG (fulmini.py);
- ogni messaggio radar riporta una riga sull'attività elettrica entro 40 km negli
  ultimi 20 minuti; la mappa mostra i lampi degli ultimi 30 minuti;
- avviso autonomo di attività elettrica (regola A): almeno 2 lampi entro 30 km negli
  ultimi 20 minuti; un nuovo avviso solo dopo 60 minuti senza lampi entro 30 km. Se
  nella stessa esecuzione parte anche un avviso radar, l'avviso fulmini è unito a quel
  messaggio (un solo messaggio). Controllato anche quando non ci sono scansioni nuove;
- stato: campi facoltativi lightning_active, lightning_last_inside, lightning_alert;
- registro: nella colonna dettaglio della riga "ultima" (o "gia_elaborata") il
  riepilogo dei lampi e avviso_fulmini=0/1;
- senza dati satellitari (chiavi assenti, errore, ritardo) il radar lavora come prima;
- con lampi entro 40 km negli ultimi 20 minuti il messaggio radar non riporta più la
  frase "Non implica un temporale" (01/11/2026).

Versione 5.1 (09/11/2026, dalla verifica settimanale del 02-08/11, su richiesta di Michele):
- un episodio L1/L2 si chiude dopo QUIET_SCANS = 4 scansioni distinte sotto soglia
  (20 minuti) invece di 2: con 2, metà delle scansioni :x5 ripete la :x0 e l'episodio
  si chiudeva e riapriva fra uno scroscio e l'altro (L1 ripetuti);
- l'avviso di avvicinamento non parte se c'è già una cella di pioggia entro 5 km
  (la pioggia è già arrivata, anche se debole): registro "avviso_avvicinamento=0
  (pioggia già entro 5 km)". Simulato sul registro 30/10-09/11: avvisi L1/L2 da 42 a 29,
  avvicinamento da 23 a 20, arrivi nuovi annunciati invariati (17 su 22).
"""
import argparse
import base64
import csv
import io
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone

import requests
import fulmini as fl
from nowcasting import (new_session, last_product_time, download_raw, observe,
                        evaluate, message, ITALY, STEP)

FILE_PATH = "state/stato_avvisi.json"
LOG_DIR = "registro"
STALE_MS = 120 * 60_000  # 120 minuti senza scansioni valide elaborate
QUIET_SCANS = 4  # scansioni distinte sotto soglia per chiudere un episodio (v5.1; prima 2)
# Scansioni nuove elaborate al massimo per esecuzione (12 x 5 min = 60 min).
MAX_NEW_SCANS = int(os.environ.get("RADAR_MAX_SCANS", "12"))
# Oltre questo ritardo il messaggio dichiara che l'avviso arriva in recupero.
LATE_NOTE_MIN = 20
MISSING = "scansione mancante"
# Avviso di avvicinamento: un nuovo avviso è ammesso se dal precedente c'è stato un
# avviso L1/L2 (l'episodio annunciato si è concretizzato) oppure se sono passati
# almeno APPROACH_COOLDOWN_MS (l'annuncio precedente non si è concretizzato).
APPROACH_COOLDOWN_MS = 120 * 60_000
MOTION_FRAMES = 7  # scansioni a 5 minuti usate per il moto (30 minuti)
RADAR_LINK = "https://mappe.protezionecivile.gov.it/it/mappe-rischi/piattaforma-radar/"
INSUFFICIENT = ("scansioni non consecutive", "copertura o cella locale insufficiente",
                "scansione mancante")
DEFAULT = {
    "version": 1, "active": False, "level": 0, "quiet": 0,
    "last_scan": 0, "last_alert_scan": 0,
}

RADII = ("5", "10", "20", "40")
LOG_FIELDS = (
    ["run_utc", "run_id", "esito", "dettaglio", "ruolo_scansione",
     "scan_utc", "scan_ora_italiana", "ritardo_scan_min",
     "livello", "motivo", "punto_mm_h"]
    + [f"{k}_{r}km" for r in RADII
       for k in ("copertura", "max_mm_h", "celle_ge1", "celle_ge5", "celle_ge10")]
    + ["notifica_inviata", "episodio_attivo", "livello_episodio", "episodio_scaduto"]
)


def decide(old, level, scan_time):
    """Restituisce (nuovo stato, notifica?, salvataggio?).

    Non invia avvisi ripetuti quando l'episodio è ancora attivo.
    Disarma dopo QUIET_SCANS scansioni successive distinte senza soglia, oppure
    d'ufficio se l'ultima scansione valida elaborata è più vecchia di STALE_MS.
    """
    state = deepcopy(old)
    if scan_time <= int(state.get("last_scan", 0)):
        return state, False, False
    if is_stale(state, scan_time):
        state.update(active=False, level=0, quiet=0, last_scan=scan_time)
        if level == 0:
            return state, False, True
        # Con livello > 0 si prosegue: l'episodio è nuovo e va notificato.
    if level > 0:
        if not state["active"] or level > state["level"]:
            state.update(active=True, level=level, quiet=0,
                         last_scan=scan_time, last_alert_scan=scan_time)
            return state, True, True
        if state["quiet"] > 0:
            state.update(quiet=0, last_scan=scan_time)
            return state, False, True
        # Aggiorna l'ultima scansione valida anche durante pioggia continua.
        # Evita che una scansione gia' elaborata venga contata due volte.
        state["last_scan"] = scan_time
        return state, False, True
    if not state["active"]:
        return state, False, False
    state["quiet"] += 1
    state["last_scan"] = scan_time
    if state["quiet"] >= QUIET_SCANS:
        state.update(active=False, level=0, quiet=0)
    return state, False, True


def is_stale(state, scan_time):
    last = int(state.get("last_scan", 0))
    return bool(state.get("active")) and last > 0 and scan_time - last > STALE_MS


# ---------------------------------------------------------------- registro

def iso_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_fields(obs, run_dt):
    """Campi del registro relativi a una scansione osservata."""
    row = {
        "scan_utc": iso_utc(obs["when"]),
        "scan_ora_italiana": datetime.fromtimestamp(obs["when"] / 1000, ITALY)
        .strftime("%d/%m/%Y %H:%M"),
        "ritardo_scan_min": round((run_dt.timestamp() * 1000 - obs["when"]) / 60_000, 1),
        "punto_mm_h": "" if obs["point"] is None else round(obs["point"], 2),
    }
    for r in RADII:
        s = obs["stats"][r]
        row[f"copertura_{r}km"] = round(s["coverage"], 3)
        row[f"max_mm_h_{r}km"] = "" if s["max_mm_h"] is None else round(s["max_mm_h"], 2)
        row[f"celle_ge1_{r}km"] = s["cells_ge_1"]
        row[f"celle_ge5_{r}km"] = s["cells_ge_5"]
        row[f"celle_ge10_{r}km"] = s["cells_ge_10"]
    return row


def build_rows(base, scans, run_dt):
    """Una riga per scansione (precedente, ultima); una sola riga se mancano.

    Formato delle versioni precedenti, mantenuto per compatibilità.
    """
    if not scans:
        return [dict(base)]
    rows = []
    for role, obs in zip(("precedente", "ultima"), scans):
        row = dict(base)
        row["ruolo_scansione"] = role
        row.update(scan_fields(obs, run_dt))
        rows.append(row)
    return rows


def time_fields(when, run_dt):
    """Campi del registro di una scansione nota solo per l'ora (non scaricata)."""
    return {
        "scan_utc": iso_utc(when),
        "scan_ora_italiana": datetime.fromtimestamp(when / 1000, ITALY)
        .strftime("%d/%m/%Y %H:%M"),
        "ritardo_scan_min": round((run_dt.timestamp() * 1000 - when) / 60_000, 1),
    }


PROCESSED_ROLES = ("ultima", "intermedia")
PROCESSED_OUTCOMES = ("ok", "dati_insufficienti")


def last_processed_in_log(text):
    """Ora (ms) dell'ultima scansione già elaborata secondo un CSV di registro."""
    best = 0
    for row in csv.DictReader(io.StringIO(text or "")):
        if row.get("ruolo_scansione") not in PROCESSED_ROLES:
            continue
        if row.get("esito") not in PROCESSED_OUTCOMES:
            continue
        try:
            dt = datetime.strptime(row["scan_utc"], "%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, TypeError, ValueError):
            continue
        best = max(best, int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000))
    return best


def read_log_text(token, path):
    resp = github_request("GET", path, token)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return base64.b64decode(resp.json()["content"]).decode("utf-8")


def last_processed_from_logs(token, run_dt):
    """Ultima scansione elaborata secondo il registro di oggi (o di ieri)."""
    from datetime import timedelta
    for day in (run_dt, run_dt - timedelta(days=1)):
        found = last_processed_in_log(read_log_text(token, log_path(day)))
        if found:
            return found
    return 0


def plan_new_times(latest, last_done, max_new=None):
    """Ore delle scansioni da elaborare, in ordine cronologico.

    Tutte quelle a passo STEP dopo last_done fino a latest, al massimo max_new
    (le più recenti). Lista vuota se latest è già stata elaborata.
    """
    max_new = MAX_NEW_SCANS if max_new is None else max_new
    times = []
    t = latest
    while t > last_done and len(times) < max_new:
        times.append(t)
        t -= STEP
    return sorted(times)


def late_note(message_text, when, run_dt):
    delay = (run_dt.timestamp() * 1000 - when) / 60_000
    if delay <= LATE_NOTE_MIN:
        return message_text
    return (message_text + f"\nNota: avviso inviato {delay:.0f} min dopo la scansione "
            "(recupero di scansioni arretrate).")


class PartialFailure(Exception):
    """Errore a metà elaborazione: conserva lo stato già consolidato."""

    def __init__(self, stage, error, state, save, rows):
        super().__init__(f"{stage}: {type(error).__name__}")
        self.stage, self.error = stage, error
        self.state, self.save, self.rows = state, save, rows


def process_scans(state, obs_by_time, new_times, run_dt, base, send):
    """Applica decide() a ogni scansione nuova, in ordine cronologico.

    obs_by_time: ora -> osservazione (None se il download non è riuscito);
    deve contenere anche la scansione che precede la prima nuova.
    send(testo, ora_scansione) invia un avviso. Restituisce (stato, salvataggio?, righe).
    Se un invio fallisce solleva PartialFailure con lo stato consolidato
    prima di quella scansione, così gli avvisi già partiti non si ripetono.
    """
    rows, save = [], False
    for i, when in enumerate(new_times):
        role = "ultima" if i == len(new_times) - 1 else "intermedia"
        cur, prev = obs_by_time.get(when), obs_by_time.get(when - STEP)
        row = dict(base, ruolo_scansione=role, notifica_inviata=0)
        if cur is None:
            row.update(time_fields(when, run_dt))
        else:
            row.update(scan_fields(cur, run_dt))
        if cur is None or prev is None:
            level, reason = 0, MISSING
        else:
            level, reason = evaluate(prev, cur)
        row.update(livello=level, motivo=reason)
        if reason in INSUFFICIENT:
            row.update(esito="dati_insufficienti",
                       episodio_attivo=int(state["active"]),
                       livello_episodio=state["level"])
            rows.append(row)
            continue
        expired = is_stale(state, when)
        next_state, alert, step_save = decide(state, level, when)
        if alert:
            try:
                send(late_note(message(cur, level, reason), when, run_dt), when)
            except Exception as error:
                row.update(esito="errore_telegram",
                           dettaglio=f"{base.get('dettaglio', '')}; {type(error).__name__}")
                rows.append(row)
                raise PartialFailure("telegram", error, state, save, rows) from error
            row["notifica_inviata"] = 1
        state, save = next_state, save or step_save
        row.update(esito="ok", episodio_attivo=int(state["active"]),
                   livello_episodio=state["level"], episodio_scaduto=int(expired))
        rows.append(row)
    return state, save, rows


def append_csv(existing_text, rows):
    """Aggiunge le righe al testo CSV esistente (con intestazione se nuovo)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LOG_FIELDS, extrasaction="ignore",
                            lineterminator="\n")
    if not existing_text:
        writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})
    text = existing_text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + buf.getvalue()


def log_path(run_dt):
    return f"{LOG_DIR}/{run_dt:%Y}/{run_dt:%Y-%m-%d}.csv"


def append_log(token, rows, run_dt):
    """Aggiunge righe al registro giornaliero; un nuovo tentativo su conflitto."""
    path = log_path(run_dt)
    for attempt in range(2):
        resp = github_request("GET", path, token)
        if resp.status_code == 404:
            text, sha = "", None
        else:
            resp.raise_for_status()
            data = resp.json()
            text = base64.b64decode(data["content"]).decode("utf-8")
            sha = data["sha"]
        payload = {
            "message": "Registro esecuzione radar [skip ci]",
            "content": base64.b64encode(append_csv(text, rows).encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        put = github_request("PUT", path, token, body=payload)
        if put.status_code in (409, 422) and attempt == 0:
            continue
        put.raise_for_status()
        return


def write_log(args, rows, run_dt):
    """Scrive il registro senza mai bloccare avvisi o stato."""
    if args.dry_run:
        print("SOLO SIMULAZIONE, righe di registro non salvate:")
        print(append_csv("", rows), end="")
        return
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("Registro non scritto: GITHUB_TOKEN assente", file=sys.stderr)
        return
    try:
        append_log(token, rows, run_dt)
        print(f"Registro aggiornato: {log_path(run_dt)}")
    except Exception as error:
        print(f"Registro non aggiornato: {type(error).__name__}", file=sys.stderr)


# ------------------------------------------------------------- GitHub/Telegram

def github_request(method, path, token, *, body=None):
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo or "/" not in repo:
        raise RuntimeError("GITHUB_REPOSITORY non definito")
    from urllib.parse import quote
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(method, url, headers=headers, json=body, timeout=20)
    return resp


def load_state(token):
    resp = github_request("GET", FILE_PATH, token)
    if resp.status_code == 404:
        return deepcopy(DEFAULT), None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    obj = json.loads(content)
    if not isinstance(obj, dict) or obj.get("version") != 1:
        raise ValueError("Versione stato non riconosciuta")
    for k, typ in [("active", bool), ("level", int), ("quiet", int),
                   ("last_scan", int), ("last_alert_scan", int)]:
        if k not in obj or type(obj[k]) is not typ:
            raise ValueError(f"Stato non valido: {k}")
    return obj, data["sha"]


def persist_state(token, state, sha):
    data = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    payload = {
        "message": "Aggiorna stato avvisi radar [skip ci]",
        "content": base64.b64encode(data.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    resp = github_request("PUT", FILE_PATH, token, body=payload)
    resp.raise_for_status()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Credenziali Telegram assenti")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text,
              "disable_web_page_preview": True},
        timeout=20,
    )
    # Non stampare eccezioni HTTP: gli URL di richiesta contengono il token.
    if resp.status_code != 200 or not resp.json().get("ok"):
        raise RuntimeError(f"Telegram non conferma l'invio (HTTP {resp.status_code})")


# --------------------------------------------------------------------- main

# ------------------------------------------------- moto, avvicinamento, mappa

def approach_allowed(state, scan_time):
    """Un solo avviso di avvicinamento per episodio.

    Mai durante un episodio L1/L2 attivo. Ammesso se non ce ne sono stati, se dopo
    l'ultimo c'è stato un avviso L1/L2, o se sono passati APPROACH_COOLDOWN_MS.
    """
    if state.get("active"):
        return False
    last = int(state.get("approach_scan", 0))
    if last <= 0:
        return True
    if int(state.get("last_alert_scan", 0)) > last:
        return True
    return scan_time - last > APPROACH_COOLDOWN_MS


def approach_message(cella, moto, when, run_dt):
    import movimento as mv
    t = datetime.fromtimestamp(when / 1000, ITALY).strftime("%d/%m/%Y %H:%M %Z")
    lo, hi = cella["arrivo_range_min"]
    n = len(moto["vettori_kmh"])
    concordi = round(cella["quota_concordi"] * n)
    testo = (f"Radar Conversano: pioggia in avvicinamento\n"
             f"Cella a {cella['dist_km']:.0f} km a {mv.punto_cardinale(cella['direzione_gradi'])}, "
             f"massimo {cella['max_mm_h']:.1f} mm/h, {mv.descrivi_moto(moto)}.\n"
             f"Arrivo stimato fra circa {cella['arrivo_min']:.0f} min "
             f"(intervallo {lo:.0f}-{hi:.0f} min); {concordi} stime su {n} la danno in arrivo.\n"
             f"Scansione: {t}\n"
             "Stima sperimentale: la cella può cambiare direzione, intensificarsi o dissolversi. "
             "Avviso non ufficiale.\n"
             f"Radar Protezione Civile: {RADAR_LINK}")
    return late_note(testo, when, run_dt)


class RadarContext:
    """Scansioni grezze della corsa, ritagli, moto e mappe (tutto best effort)."""

    def __init__(self, session, raws):
        self.session, self.raws = session, raws
        self.crops, self.motions = {}, {}

    def crop(self, when):
        if when not in self.crops:
            import movimento as mv
            raw = self.raws.get(when)
            self.crops[when] = None if raw is None else mv.ritaglia(raw)
        return self.crops[when]

    def rain_nearby(self, when):
        import movimento as mv
        c = self.crop(when)
        return bool(c) and bool(mv.celle(c[0], c[1], c[2], None))

    def fetch_motion_frames(self, latest):
        """Scarica le scansioni degli ultimi 30 minuti che mancano (best effort)."""
        for k in range(1, MOTION_FRAMES):
            when = latest - k * STEP
            if when not in self.raws:
                try:
                    self.raws[when] = download_raw(self.session, when)
                except Exception:
                    self.raws[when] = None

    def motion(self, when):
        if when not in self.motions:
            import movimento as mv
            frames = []
            for k in range(MOTION_FRAMES - 1, -1, -1):
                c = self.crop(when - k * STEP) if (when - k * STEP) in self.raws else None
                frames.append(None if c is None else c[0])
            self.motions[when] = mv.stima_moto(frames)
        return self.motions[when]

    def analysis(self, when):
        """(ritaglio, moto, celle) per la scansione when; None se manca il ritaglio."""
        import movimento as mv
        c = self.crop(when)
        if c is None:
            return None
        moto = self.motion(when)
        return c, moto, mv.celle(c[0], c[1], c[2], moto)

    def map_png(self, when, titolo, lampi=None):
        import mappa
        a = self.analysis(when)
        if a is None:
            return None, None
        (campo, r, cc, geo), moto, cl = a
        ora = datetime.fromtimestamp(when / 1000, ITALY).strftime("%d/%m/%Y %H:%M")
        import movimento as mv
        full = f"{titolo} – scansione {ora}\npioggia {mv.descrivi_moto(moto)}"
        return mappa.disegna(campo, r, cc, geo, full, moto, cl, lampi=lampi), moto


CAPTION_MAX = 1024  # limite Telegram per la didascalia di una foto
FRASE_NO_TEMPORALE = "Non implica un temporale. "  # vedi nowcasting.message()


def is_lightning_alert(text):
    return text.split("\n", 1)[0].startswith("Radar Conversano: attività elettrica")


def make_sender(ctx, text_sender, photo_sender, log=print, att=None, avviso=None):
    """send(testo, ora): allega la mappa e il moto; ripiega sul solo testo.

    att (fulmini.Attivita): aggiunge la riga sull'attività elettrica e i lampi sulla mappa.
    avviso: {"pendente": bool, "inviato": bool}; se pendente, il primo messaggio radar
    inviato include l'avviso di attività elettrica e "inviato" diventa True.
    """
    def send(text, when):
        unito = False
        if att is not None and not is_lightning_alert(text):
            if avviso and avviso.get("pendente") and not avviso.get("inviato") and att.disponibile:
                text = fl.aggiungi_avviso_a_messaggio(text, att)
                unito = True
            else:
                text = text.replace("\nScansione:", f"\n{att.riga()}\nScansione:", 1)
        # Con lampi entro 40 km la frase del radar "Non implica un temporale" non vale più
        # (scelta di Michele, 01/11/2026).
        if att is not None and att.disponibile and att.conta(fl.RAGGIO_RIGA_KM) > 0:
            text = text.replace(FRASE_NO_TEMPORALE, "")
        lampi = att.per_mappa() if att is not None and att.disponibile else None
        png = None
        try:
            png, moto = ctx.map_png(when, "Radar Conversano PGL320", lampi=lampi)
            if moto and "Cella a " not in text:  # l'avviso di avvicinamento ha già il moto
                import movimento as mv
                text = text.replace("\nScansione:", f"\nPioggia {mv.descrivi_moto(moto)}.\nScansione:", 1)
        except Exception as error:
            log(f"Mappa non prodotta: {type(error).__name__}")
            png = None
        sent = False
        if png:
            try:
                if len(text) <= CAPTION_MAX:
                    photo_sender(png, text)
                    sent = True
                else:  # didascalia breve sulla foto, testo completo a parte
                    photo_sender(png, text.split("\n", 1)[0])
            except Exception as error:
                log(f"Foto non inviata ({type(error).__name__}): invio il solo testo")
        if not sent:
            text_sender(text)
        if unito:
            avviso["inviato"] = avviso["unito"] = True
    return send


def send_telegram_photo(png, caption):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Credenziali Telegram assenti")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat, "caption": caption[:1024]},
        files={"photo": ("radar.png", png, "image/png")},
        timeout=30,
    )
    if resp.status_code != 200 or not resp.json().get("ok"):
        raise RuntimeError(f"Telegram non conferma la foto (HTTP {resp.status_code})")


def evaluate_approach(ctx, state, latest, run_dt, send_approach):
    """Valuta e, se del caso, invia l'avviso di avvicinamento.

    Restituisce (stato aggiornato, salvataggio?, testo per il registro).
    """
    import movimento as mv
    a = ctx.analysis(latest)
    if a is None:
        return state, False, "moto=scansione non disponibile"
    _, moto, cl = a
    info = f"moto={mv.descrivi_moto(moto)}"
    if moto:
        info += f" (dispersione {moto['dispersione_kmh']:.0f} km/h, {len(moto['vettori_kmh'])} vettori)"
    cella = mv.cella_in_avvicinamento(cl)
    if cella is None:
        return state, False, info
    lo, hi = cella["arrivo_range_min"]
    info += (f"; avvicinamento={cella['arrivo_min']:.0f} min ({lo:.0f}-{hi:.0f}), "
             f"{cella['dist_km']:.0f} km {mv.punto_cardinale(cella['direzione_gradi'])}, "
             f"{cella['max_mm_h']:.1f} mm/h, concordi {cella['quota_concordi']:.2f}")
    if any(c["dist_km"] <= mv.RAGGIO_PASSAGGIO for c in cl):
        # la pioggia è già entro 5 km: non è più "in avvicinamento" (v5.1)
        return state, False, info + "; avviso_avvicinamento=0 (pioggia già entro 5 km)"
    if not approach_allowed(state, latest):
        return state, False, info + "; avviso_avvicinamento=0 (già inviato o episodio in corso)"
    send_approach(approach_message(cella, moto, latest, run_dt), latest)
    new_state = dict(state, approach_scan=latest)
    return new_state, True, info + "; avviso_avvicinamento=1"


def fetch_observations(session, new_times):
    """Scarica le scansioni nuove e quella che precede la prima.

    Restituisce (osservazioni, file grezzi, orari mancanti). Un download non
    riuscito vale None (coppia insufficiente), tranne per la scansione più
    recente: in quel caso l'errore si propaga.
    """
    obs, raws, missing = {}, {}, []
    wanted = [new_times[0] - STEP] + list(new_times)
    for when in wanted:
        try:
            raws[when] = download_raw(session, when)
            obs[when] = observe(raws[when], when)
        except Exception:
            if when == new_times[-1]:
                raise
            obs[when], raws[when] = None, None
            missing.append(datetime.fromtimestamp(when / 1000, ITALY).strftime("%H:%M"))
    return obs, raws, missing


def start_lightning(run_dt):
    """Avvia lo scaricamento dei lampi; senza chiavi restituisce un risultato vuoto."""
    if not (os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
            and os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()):
        class Assente:
            def risultato(self, ora):
                return fl.Attivita.non_disponibile("chiavi EUMETSAT assenti")
        return Assente()
    return fl.Scaricamento(run_dt)


def lightning_changed(old, new):
    return any(old.get(k, v) != new.get(k, v) for k, v in fl.STATO_INIZIALE.items())


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Valuta radar e stato, nessun messaggio né scrittura")
    group.add_argument("--notify", action="store_true",
                       help="Invia gli avvisi, salva stato e registro nel repository")
    args = parser.parse_args()

    run_dt = datetime.now(timezone.utc)
    event = os.environ.get("GITHUB_EVENT_NAME", "locale")
    base = {"run_utc": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "notifica_inviata": 0, "dettaglio": f"evento={event}"}
    rows = []
    stage = "radar"
    scaricamento = start_lightning(run_dt)
    try:
        session = new_session()
        latest = last_product_time(session)

        stage = "stato"
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        log_done = 0
        if not github_token:
            if args.notify:
                raise RuntimeError("GITHUB_TOKEN assente: avvisi disabilitati")
            state, sha = deepcopy(DEFAULT), None
            print("Controllo senza accesso allo stato GitHub")
        else:
            state, sha = load_state(github_token)
            try:
                log_done = last_processed_from_logs(github_token, run_dt)
            except Exception as error:
                # Senza registro si riparte dallo stato: al più si rielaborano
                # scansioni già viste, senza effetti (vedi decide()).
                print(f"Registro non letto: {type(error).__name__}", file=sys.stderr)
        last_done = max(int(state["last_scan"]), log_done)
        new_times = plan_new_times(latest, last_done)

        if not new_times:
            row = dict(base, esito="nessuna_novita", ruolo_scansione="gia_elaborata",
                       episodio_attivo=int(state["active"]),
                       livello_episodio=state["level"])
            row.update(time_fields(latest, run_dt))
            rows = [row]
            print("Nessuna scansione nuova dopo "
                  f"{datetime.fromtimestamp(last_done / 1000, ITALY):%d/%m %H:%M}.")
            # Attività elettrica anche senza scansioni nuove (mappa sull'ultima scansione).
            stage = "fulmini"
            att = scaricamento.risultato(run_dt)
            print(att.riga())
            f_state, f_alert = fl.valuta_avviso(state, att)
            note = att.registro()
            if f_alert:
                ctx = RadarContext(session, {})
                try:
                    ctx.raws[latest] = download_raw(session, latest)
                except Exception:
                    ctx.raws[latest] = None
                sent_dry = []
                if args.dry_run:
                    send = make_sender(ctx, sent_dry.append, lambda png, t: sent_dry.append(t), att=att)
                else:
                    send = make_sender(ctx, send_telegram, send_telegram_photo, att=att)
                try:
                    send(fl.testo_avviso(att, latest, RADAR_LINK), latest)
                    note += "; avviso_fulmini=1"
                    for text in sent_dry:
                        print("SOLO SIMULAZIONE, avviso non inviato:\n" + text)
                except Exception as error:
                    note += f"; avviso_fulmini=errore {type(error).__name__}"
                    f_state = {k: state.get(k, v) for k, v in fl.STATO_INIZIALE.items()}
            else:
                note += "; avviso_fulmini=0"
            row["dettaglio"] = f"{row['dettaglio']}; {note}"
            new_state = dict(state, **f_state)
            if not args.dry_run and lightning_changed(state, new_state):
                stage = "salvataggio_stato"
                persist_state(github_token, new_state, sha)
                print("Stato fulmini salvato nel repository")
            write_log(args, rows, run_dt)
            return

        stage = "radar"
        obs, raws, missing = fetch_observations(session, new_times)
        base["dettaglio"] += f"; nuove={len(new_times)}"
        if missing:
            base["dettaglio"] += "; mancanti=" + ",".join(missing)

        stage = "moto"
        ctx = RadarContext(session, raws)
        latest = new_times[-1]
        motion_note = ""
        try:
            if ctx.rain_nearby(latest):
                ctx.fetch_motion_frames(latest)
        except Exception as error:
            motion_note = f"moto=errore {type(error).__name__}"

        stage = "fulmini"
        att = scaricamento.risultato(run_dt)
        print(att.riga())
        f_state, f_alert = fl.valuta_avviso(state, att)
        avviso = {"pendente": f_alert, "inviato": False}
        lightning_note = att.registro()

        stage = "elaborazione"
        if args.dry_run:
            sent = []
            save_dir = os.environ.get("RADAR_SALVA_MAPPE", "")

            def dry_text(text):
                sent.append(text)

            def dry_photo(png, text):
                sent.append(text)
                if save_dir:
                    path = os.path.join(save_dir, f"mappa_{len(sent)}.png")
                    with open(path, "wb") as fh:
                        fh.write(png)
                    print(f"Mappa salvata: {path}")
            send = make_sender(ctx, dry_text, dry_photo, att=att, avviso=avviso)
        else:
            send = make_sender(ctx, send_telegram, send_telegram_photo, att=att, avviso=avviso)
        try:
            next_state, should_save, rows = process_scans(
                state, obs, new_times, run_dt, base, send)
        except PartialFailure as failure:
            rows = failure.rows
            if failure.save and not args.dry_run:
                persist_state(github_token, failure.state, sha)
                print("Stato consolidato salvato prima dell'errore")
            stage = failure.stage
            raise failure.error

        # Avviso di avvicinamento sull'ultima scansione (non blocca mai L1/L2).
        if not motion_note:
            try:
                next_state, approach_save, motion_note = evaluate_approach(
                    ctx, next_state, latest, run_dt, send)
                should_save = should_save or approach_save
            except Exception as error:
                motion_note = f"moto=errore {type(error).__name__}"

        # Avviso di attività elettrica non già unito a un messaggio radar.
        if f_alert and not avviso["inviato"]:
            try:
                send(fl.testo_avviso(att, latest, RADAR_LINK), latest)
                avviso["inviato"] = True
            except Exception as error:
                lightning_note += f"; avviso_fulmini=errore {type(error).__name__}"
        if f_alert and avviso["inviato"]:
            lightning_note += "; avviso_fulmini=1" + (" (unito al messaggio radar)"
                                                      if avviso.get("unito") else "")
        elif not f_alert:
            lightning_note += "; avviso_fulmini=0"
        if not f_alert or avviso["inviato"]:
            before = dict(next_state)
            next_state = dict(next_state, **f_state)
            should_save = should_save or lightning_changed(before, next_state)
        motion_note = "; ".join(x for x in (motion_note, lightning_note) if x)
        if rows and rows[-1].get("ruolo_scansione") == "ultima" and motion_note:
            rows[-1]["dettaglio"] = f"{rows[-1].get('dettaglio', '')}; {motion_note}"

        for row in rows:
            print(f"{row['scan_ora_italiana']} {row['ruolo_scansione']}: "
                  f"livello={row.get('livello')}, {row.get('motivo')}, "
                  f"notifica={row.get('notifica_inviata')}")
        if args.dry_run:
            print(f"SOLO SIMULAZIONE: {len(sent)} avvisi non inviati, "
                  f"salvataggio={should_save}, episodio_attivo={next_state['active']}")
            for text in sent:
                print("---\n" + text)
            write_log(args, rows, run_dt)
            return
        if should_save:
            stage = "salvataggio_stato"
            persist_state(github_token, next_state, sha)
            print("Stato radar salvato nel repository")
        else:
            print("Nessuna nuova notifica e nessun aggiornamento dello stato")
        write_log(args, rows, run_dt)
    except Exception as error:
        if not rows or not str(rows[-1].get("esito", "")).startswith("errore"):
            rows = rows + [dict(base, esito=f"errore_{stage}",
                                dettaglio=f"{base['dettaglio']}; {type(error).__name__}")]
        write_log(args, rows, run_dt)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Nei messaggi d'errore non riportare URL/credenziali.
        print(f"Elaborazione non completata: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
