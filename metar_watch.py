"""Sorveglianza temporali METAR di Gioia del Colle (LIBV) e Bari Palese (LIBD).

Sperimentale, NON allerta ufficiale. Avvisa via Telegram solo all'inizio di un
nuovo episodio temporalesco (o all'arrivo sull'aeroporto di un temporale prima
segnalato nelle vicinanze), con le stesse regole dell'ex attività Claude oraria.

  python metar_watch.py --dry-run   legge e valuta, nessun invio né scrittura
  python metar_watch.py --notify    invia su Telegram e salva lo stato

Stato: state/stato_metar.json con l'ora (ms UTC) dell'ultima osservazione già
notificata per ciascun aeroporto; si aggiorna solo quando parte un avviso.
Un errore di lettura dei METAR non fa fallire il job: l'aeroporto risulta
"NON VERIFICABILE" (che non significa "nessun temporale").
"""
import argparse
import base64
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ROME = ZoneInfo("Europe/Rome")
URL = "https://aviationweather.gov/api/data/metar"
HEADERS = {"User-Agent": "NowcastingConversano/0.3 (PGL320, sperimentale)"}
AIRPORTS = {"LIBV": "Gioia del Colle", "LIBD": "Bari Palese"}
STATE_PATH = "state/stato_metar.json"
DEFAULT_STATE = {"version": 1, "LIBV": 0, "LIBD": 0}
STOP = {"RMK", "NOSIG", "BECMG", "TEMPO"}
FRESH_MIN = 90      # ultimo METAR accettato come fresco
NEW_MIN = 65        # finestra delle osservazioni "nuove"
PAST_MIN = 180      # finestra precedente usata per la continuità
RADAR = "https://mappe.protezionecivile.gov.it/it/mappe-rischi/piattaforma-radar"

EV_ARRIVED = "ARRIVATO"
EV_NEW_AT = "NUOVO_AT"
EV_NEW_VC = "NUOVO_VC"


# ------------------------------------------------------------------ lettura

def parse(line, now):
    """Analizza una riga METAR/SPECI; restituisce un dict o None."""
    t = line.split()
    if len(t) < 3 or t[0] not in ("METAR", "SPECI"):
        return None
    i = 2 if t[1] == "COR" else 1
    if len(t) < i + 2:
        return None
    m = re.fullmatch(r"(\d\d)(\d\d)(\d\d)Z", t[i + 1])
    if not m:
        return None
    d, h, mi = map(int, m.groups())
    y, mo = now.year, now.month
    if d > now.day:
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    try:
        ts = datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
    except ValueError:
        return None
    body = []
    for x in t[i + 2:]:
        if x in STOP:
            break
        body.append(x)
    at = [x for x in body if re.fullmatch(r"[-+]?TS[A-Z]*", x)]
    st = "AT" if at else ("VC" if "VCTS" in body else "NONE")
    return {"icao": t[i], "ts": ts, "st": st, "at": at, "body": body,
            "cor": t[1] == "COR",
            "re": [x for x in body if re.fullmatch(r"RE[A-Z]*TS[A-Z]*", x)],
            "raw": " ".join(line.split())}


def group_obs(lines, now):
    """Osservazioni per aeroporto, ordinate; una sola per ora (vale la COR)."""
    obs = {}
    for line in lines:
        o = parse(line, now)
        if not o or o["icao"] not in AIRPORTS:
            continue
        slot = obs.setdefault(o["icao"], {})
        old = slot.get(o["ts"])
        if old is None or o["cor"] or not old["cor"]:
            slot[o["ts"]] = o
    return {k: sorted(v.values(), key=lambda o: o["ts"]) for k, v in obs.items()}


def mins(a, b):
    return (a - b).total_seconds() / 60


def analyse_airport(s, now, last_notified_ms):
    """Restituisce (stato, evento, osservazione, verificabile_inizio)."""
    if not s or mins(now, s[-1]["ts"]) > FRESH_MIN:
        return "NON VERIFICABILE", None, None, None
    status = "nessun nuovo evento"
    for k, o in enumerate(s):
        if mins(now, o["ts"]) > NEW_MIN or o["st"] == "NONE":
            continue
        if o["ts"].timestamp() * 1000 <= last_notified_ms:
            status = "in corso (già notificato)"
            continue
        P = [p for p in s[:k] if 0 < mins(o["ts"], p["ts"]) <= PAST_MIN]
        ok = (k > 0 and mins(o["ts"], s[k - 1]["ts"]) <= 90
              and any(mins(o["ts"], p["ts"]) >= 150 for p in P))
        any_at = any(p["st"] == "AT" for p in P)
        any_vc = any(p["st"] == "VC" for p in P)
        any_re = any(p["re"] for p in P) or bool(o["re"])
        if o["st"] == "AT" and not any_at and any_vc:
            return "evento", EV_ARRIVED, o, True
        if not any_at and not any_vc and not any_re:
            return "evento", (EV_NEW_AT if o["st"] == "AT" else EV_NEW_VC), o, ok
        status = "in corso (già presente nelle 3 ore precedenti)"
    if status == "nessun nuovo evento" and any(
            o["re"] for o in s if mins(now, o["ts"]) <= NEW_MIN):
        status = "solo RETS (temporale recente, non notificato)"
    return status, None, None, None


# ---------------------------------------------------------------- decodifica

CARD = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
PRECIP = {"RA": "pioggia", "DZ": "pioviggine", "SN": "neve", "GR": "grandine",
          "GS": "gragnola", "PL": "granuli di ghiaccio", "SG": "neve granulosa",
          "IC": "aghi di ghiaccio", "UP": "precipitazione non identificata"}


def decode_ts(group):
    m = re.fullmatch(r"([-+]?)TS([A-Z]*)", group)
    if not m:
        return group
    inten = {"-": "debole", "+": "forte", "": "moderato"}[m.group(1)]
    codes = [m.group(2)[i:i + 2] for i in range(0, len(m.group(2)), 2)]
    names = [PRECIP.get(c, c) for c in codes if c]
    if not names:
        return f"temporale {inten}, senza precipitazione segnalata"
    if len(names) > 1:
        what = ", ".join(names[:-1]) + " e " + names[-1]
    else:
        what = names[0]
    return f"temporale {inten} con {what}"


def kmh(kt):
    return round(int(kt) * 1.852)


def describe(o, prev):
    """Righe descrittive ricavate SOLO dai gruppi presenti nel METAR."""
    out = []
    body = o["body"]
    if o["at"]:
        out.append("Fenomeno: " + "; ".join(decode_ts(g) for g in o["at"]))
    elif o["st"] == "VC":
        out.append("Fenomeno: temporale nelle vicinanze dell'aeroporto (VCTS)")
    for g in body:
        m = re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT", g)
        if m:
            if m.group(1) == "VRB":
                txt = f"Vento: variabile, {kmh(m.group(2))} km/h"
            else:
                deg = int(m.group(1))
                txt = f"Vento: da {deg}° ({CARD[round(deg / 22.5) % 16]}), {kmh(m.group(2))} km/h"
            if m.group(3):
                txt += f", raffiche {kmh(m.group(3))} km/h"
            out.append(txt)
            break
    for g in body:
        if g == "CAVOK":
            out.append("Visibilità: CAVOK (10 km o più, nessuna nube significativa)")
            break
        if re.fullmatch(r"\d{4}", g):
            out.append("Visibilità: 10 km o più" if g == "9999"
                       else f"Visibilità: {int(g)} m")
            break
    cb = [g for g in body if re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)\d{3}(CB|TCU)", g)]
    if cb:
        out.append("Nubi convettive: " + ", ".join(
            g.replace("TCU", " (cumuli torreggianti)").replace("CB", " (cumulonembi)")
            for g in cb))
    temp = temp_of(o)
    if temp is not None:
        txt = f"Temperatura: {temp} °C"
        if prev is not None and temp_of(prev) is not None:
            delta = temp - temp_of(prev)
            hhmm = prev["ts"].astimezone(ROME).strftime("%H:%M")
            txt += f" ({delta:+d} °C rispetto al METAR delle {hhmm})"
        out.append(txt)
    for g in body:
        m = re.fullmatch(r"Q(\d{4})", g)
        if m:
            out.append(f"Pressione QNH: {int(m.group(1))} hPa")
            break
    return out


def temp_of(o):
    for g in o["body"]:
        m = re.fullmatch(r"(M?\d\d)/(M?\d\d|//)", g)
        if m:
            v = m.group(1)
            return -int(v[1:]) if v.startswith("M") else int(v)
    return None


EVENT_TEXT = {
    EV_NEW_AT: "NUOVO TEMPORALE presso l'aeroporto",
    EV_NEW_VC: "NUOVO TEMPORALE nelle vicinanze dell'aeroporto",
    EV_ARRIVED: "TEMPORALE ARRIVATO SULL'AEROPORTO (prima segnalato nelle vicinanze)",
}


def event_label(ev, ok):
    if ev in (EV_NEW_AT, EV_NEW_VC) and not ok:
        where = "presso l'aeroporto" if ev == EV_NEW_AT else "nelle vicinanze"
        return (f"POSSIBILE nuovo temporale {where} – inizio non verificabile "
                "(mancano osservazioni precedenti): non è un nuovo evento certo")
    return EVENT_TEXT[ev]


def status_line(icao, status, s):
    name = AIRPORTS[icao]
    if status == "NON VERIFICABILE":
        if s:
            hh = s[-1]["ts"].astimezone(ROME).strftime("%d/%m %H:%M")
            return f"{name}: NON VERIFICABILE (ultimo METAR {hh}, troppo vecchio)"
        return f"{name}: NON VERIFICABILE (nessun METAR ricevuto)"
    last = s[-1]
    hh = last["ts"].astimezone(ROME).strftime("%H:%M")
    now_state = {"AT": "TS in atto", "VC": "VCTS nelle vicinanze"}.get(
        last["st"], "RETS (temporale recente)" if last["re"] else "nessun TS")
    return f"{name}: ultimo METAR {hh}, {now_state}; {status}"


def build_message(events, obs, statuses):
    names = [AIRPORTS[k] for k in events]
    first = min(e[1]["ts"] for e in events.values()).astimezone(ROME).strftime("%H:%M")
    if len(names) == 2:
        title = f"Temporali a Gioia del Colle e Bari, ore {first}"
    else:
        title = f"Temporale a {names[0]}, ore {first}"
    lines = [title, ""]
    for icao, (ev, o, ok) in events.items():
        s = obs[icao]
        k = s.index(o)
        prev = s[k - 1] if k > 0 else None
        hh = o["ts"].astimezone(ROME).strftime("%d/%m %H:%M")
        lines.append(f"{AIRPORTS[icao]} ({icao}), osservazione delle {hh}")
        lines.append(event_label(ev, ok))
        lines.extend(describe(o, prev))
        lines.append("METAR: " + o["raw"])
        lines.append("")
    for icao in AIRPORTS:
        if icao not in events:
            lines.append("Altro aeroporto – " + status_line(icao, statuses[icao], obs.get(icao)))
    lines.append("")
    lines.append("Dato osservato dagli aeroporti, non a Conversano: direzione e tempi di "
                 "arrivo non sono noti dai soli METAR. Avviso sperimentale non ufficiale.")
    lines.append("Per vedere se si avvicina a Conversano guarda il radar: " + RADAR)
    return "\n".join(lines)


# ------------------------------------------------------------ rete e stato

def fetch_lines():
    r = requests.get(URL, params={"ids": "LIBV,LIBD", "format": "raw", "hours": 5},
                     headers=HEADERS, timeout=20)
    if r.status_code == 204:
        return []
    r.raise_for_status()
    return [l for l in r.text.splitlines() if l.strip()]


def load_state(token):
    from alert_runner import github_request
    resp = github_request("GET", STATE_PATH, token)
    if resp.status_code == 404:
        return deepcopy(DEFAULT_STATE), None
    resp.raise_for_status()
    data = resp.json()
    obj = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    if obj.get("version") != 1 or not all(type(obj.get(k)) is int for k in AIRPORTS):
        raise ValueError("Stato METAR non valido")
    return obj, data["sha"]


def save_state(token, state, sha):
    from alert_runner import github_request
    payload = {"message": "Aggiorna stato avvisi METAR [skip ci]",
               "content": base64.b64encode(
                   (json.dumps(state, indent=2) + "\n").encode("utf-8")).decode("ascii")}
    if sha:
        payload["sha"] = sha
    github_request("PUT", STATE_PATH, token, body=payload).raise_for_status()


def evaluate_all(lines, now, state):
    obs = group_obs(lines, now)
    events, statuses = {}, {}
    for icao in AIRPORTS:
        status, ev, o, ok = analyse_airport(obs.get(icao, []), now, state.get(icao, 0))
        statuses[icao] = status
        if ev:
            events[icao] = (ev, o, ok)
    return obs, events, statuses


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--notify", action="store_true")
    args = p.parse_args()
    now = datetime.now(timezone.utc)
    try:
        lines = fetch_lines()
    except Exception as error:
        print(f"METAR non leggibili ({type(error).__name__}): LIBV e LIBD NON VERIFICABILI. "
              "Nessun avviso possibile in questa esecuzione.")
        return
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        state, sha = load_state(token)
    elif args.notify:
        raise RuntimeError("GITHUB_TOKEN assente")
    else:
        state, sha = deepcopy(DEFAULT_STATE), None
    obs, events, statuses = evaluate_all(lines, now, state)
    for icao in AIRPORTS:
        print(status_line(icao, statuses[icao], obs.get(icao)))
    if not events:
        print("Nessun nuovo temporale: nessun avviso.")
        return
    text = build_message(events, obs, statuses)
    if args.dry_run:
        print("SOLO SIMULAZIONE, messaggio non inviato:\n" + text)
        return
    from alert_runner import send_telegram
    send_telegram(text)
    print("Telegram: avviso METAR accettato")
    new_state = dict(state)
    for icao, (_, o, _) in events.items():
        new_state[icao] = int(o["ts"].timestamp() * 1000)
    save_state(token, new_state, sha)
    print("Stato METAR salvato")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Controllo METAR non completato: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
