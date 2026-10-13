"""Avvisi radar per PGL320 con stato persistito nei file del repository.

L'uso --dry-run non invia messaggi né modifica lo stato.
L'uso --notify richiede GITHUB_TOKEN e i segreti Telegram.
"""
import argparse
import base64
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone

import requests
from nowcasting import get_scans, evaluate, message, ITALY

FILE_PATH = "state/stato_avvisi.json"
DEFAULT = {
    "version": 1, "active": False, "level": 0, "quiet": 0,
    "last_scan": 0, "last_alert_scan": 0,
}


def decide(old, level, scan_time):
    """Restituisce (nuovo stato, notifica?, salvataggio?).

    Non invia avvisi ripetuti quando l'episodio è ancora attivo.
    Disarma solo dopo due scansioni successive distinte senza soglia.
    """
    state = deepcopy(old)
    if scan_time <= int(state.get("last_scan", 0)):
        return state, False, False
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


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Valuta radar e stato, nessun messaggio né scrittura")
    group.add_argument("--notify", action="store_true",
                       help="Invia gli avvisi, salva stato nel repository")
    args = parser.parse_args()
    a, b = get_scans()
    level, reason = evaluate(a, b)
    timestamp = datetime.fromtimestamp(b["when"]/1000, ITALY)
    print(f"Radar {timestamp.isoformat()} PGL320={b['point']} "
          f"livello={level}, {reason}")

    if reason in ("scansioni non consecutive", "copertura o cella locale insufficiente"):
        print("Dati insufficienti; stato non modificato.")
        return
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        if args.notify:
            raise RuntimeError("GITHUB_TOKEN assente: avvisi disabilitati")
        state, sha = deepcopy(DEFAULT), None
        print("Controllo senza accesso allo stato GitHub")
    else:
        state, sha = load_state(github_token)

    next_state, should_alert, should_save = decide(state, level, b["when"])
    if args.dry_run:
        print(f"SOLO SIMULAZIONE: notifica={should_alert}, "
              f"salvataggio={should_save}, episodio_attivo={next_state['active']}")
        return
    if should_alert:
        send_telegram(message(b, level, reason))
        print("Telegram: messaggio accettato")
    if should_save:
        persist_state(github_token, next_state, sha)
        print("Stato radar salvato nel repository")
    else:
        print("Nessuna nuova notifica e nessun aggiornamento dello stato")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Nei messaggi d'errore non riportare URL/credenziali.
        print(f"Elaborazione non completata: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
