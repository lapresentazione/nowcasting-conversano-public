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
from nowcasting import get_scans, evaluate, message, ITALY

FILE_PATH = "state/stato_avvisi.json"
LOG_DIR = "registro"
STALE_MS = 60 * 60_000  # 60 minuti senza scansioni valide elaborate
INSUFFICIENT = ("scansioni non consecutive", "copertura o cella locale insufficiente")
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
    Disarma dopo due scansioni successive distinte senza soglia, oppure
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
    if state["quiet"] >= 2:
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
    """Una riga per scansione (precedente, ultima); una sola riga se mancano."""
    if not scans:
        return [dict(base)]
    rows = []
    for role, obs in zip(("precedente", "ultima"), scans):
        row = dict(base)
        row["ruolo_scansione"] = role
        row.update(scan_fields(obs, run_dt))
        rows.append(row)
    return rows


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

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Valuta radar e stato, nessun messaggio né scrittura")
    group.add_argument("--notify", action="store_true",
                       help="Invia gli avvisi, salva stato e registro nel repository")
    args = parser.parse_args()

    run_dt = datetime.now(timezone.utc)
    base = {"run_utc": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "notifica_inviata": 0}
    scans = []
    stage = "radar"
    try:
        a, b = get_scans()
        scans = [a, b]
        level, reason = evaluate(a, b)
        base.update(livello=level, motivo=reason)
        timestamp = datetime.fromtimestamp(b["when"] / 1000, ITALY)
        print(f"Radar {timestamp.isoformat()} PGL320={b['point']} "
              f"livello={level}, {reason}")

        if reason in INSUFFICIENT:
            print("Dati insufficienti; stato non modificato.")
            base["esito"] = "dati_insufficienti"
            write_log(args, build_rows(base, scans, run_dt), run_dt)
            return

        stage = "stato"
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not github_token:
            if args.notify:
                raise RuntimeError("GITHUB_TOKEN assente: avvisi disabilitati")
            state, sha = deepcopy(DEFAULT), None
            print("Controllo senza accesso allo stato GitHub")
        else:
            state, sha = load_state(github_token)

        expired = is_stale(state, b["when"])
        next_state, should_alert, should_save = decide(state, level, b["when"])
        base.update(episodio_attivo=int(next_state["active"]),
                    livello_episodio=next_state["level"],
                    episodio_scaduto=int(expired))
        if args.dry_run:
            print(f"SOLO SIMULAZIONE: notifica={should_alert}, "
                  f"salvataggio={should_save}, episodio_attivo={next_state['active']}, "
                  f"episodio_scaduto={expired}")
            base["esito"] = "ok"
            write_log(args, build_rows(base, scans, run_dt), run_dt)
            return
        if should_alert:
            stage = "telegram"
            send_telegram(message(b, level, reason))
            base["notifica_inviata"] = 1
            print("Telegram: messaggio accettato")
        if should_save:
            stage = "salvataggio_stato"
            persist_state(github_token, next_state, sha)
            print("Stato radar salvato nel repository")
        else:
            print("Nessuna nuova notifica e nessun aggiornamento dello stato")
        base["esito"] = "ok"
        write_log(args, build_rows(base, scans, run_dt), run_dt)
    except Exception as error:
        base.update(esito=f"errore_{stage}", dettaglio=type(error).__name__)
        write_log(args, build_rows(base, scans, run_dt), run_dt)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Nei messaggi d'errore non riportare URL/credenziali.
        print(f"Elaborazione non completata: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
