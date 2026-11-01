"""Prova dei fulmini MTG Lightning Imager (EUMETSAT) per la PGL320. Sola lettura.

Non invia messaggi e non tocca stato o registro del sistema radar.
Richiede i segreti EUMETSAT_CONSUMER_KEY e EUMETSAT_CONSUMER_SECRET (mai stampati).

Intervallo: di default l'ultima ora e mezza; con le variabili DAL e AL (ora italiana,
"AAAA-MM-GG HH:MM", al massimo 6 ore) si rilegge un periodo passato dall'archivio.

Cosa stampa:
1. se l'autenticazione funziona;
2. i pacchetti LI-2-LFL (flash, disco intero, 10 minuti) dell'intervallo,
   con ora di pubblicazione e ritardo rispetto alla fine della finestra;
3. la struttura del file (variabili, dimensioni, unità) del primo pacchetto;
4. i flash entro 100 km dalla stazione, con ora italiana, distanza e direzione,
   e un conteggio per fasce di distanza (più quelli entro 300 km);
5. (v2, 01/11/2026) il conteggio a 5 minuti dei flash entro 40 km, per fasce di distanza,
   con il flash più vicino di ogni intervallo: serve al confronto con il WH57.
Conteggi ed elenchi usano solo i flash compresi nell'intervallo richiesto.
"""
import io
import math
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

LAT, LON = 40.976, 17.13
ROMA = ZoneInfo("Europe/Rome")
COLLEZIONE = "EO:EUM:DAT:0691"   # LI Lightning Flashes, MTG, 0 gradi
EPOCA = datetime(2000, 1, 1, tzinfo=timezone.utc)
FASCE = (10, 20, 40, 100, 300)
PUNTI = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]


def distanza_direzione(lat, lon):
    """Distanza (km) e azimut (gradi) dalla stazione, formula dell'emisenoverso."""
    r = 6371.0
    p1, p2 = math.radians(LAT), np.radians(lat)
    dphi = p2 - p1
    dl = np.radians(lon) - math.radians(LON)
    a = np.sin(dphi / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    d = 2 * r * np.arcsin(np.sqrt(a))
    y = np.sin(dl) * np.cos(p2)
    x = math.cos(p1) * np.sin(p2) - math.sin(p1) * np.cos(p2) * np.cos(dl)
    az = (np.degrees(np.arctan2(y, x)) + 360) % 360
    return d, az


def trova(ds, nome):
    """Cerca una variabile per nome nel gruppo radice e nei sottogruppi."""
    if nome in ds.variables:
        return ds.variables[nome]
    for g in ds.groups.values():
        v = trova(g, nome)
        if v is not None:
            return v
    return None


def struttura(ds, prefisso=""):
    righe = []
    for n, v in ds.variables.items():
        righe.append(f"  {prefisso}{n} {v.dimensions} {getattr(v, 'units', '')}")
    for gn, g in ds.groups.items():
        righe += struttura(g, prefisso + gn + "/")
    return righe


def leggi_flash(contenuto_nc):
    """Bytes di un file netCDF LFL -> (tempi UTC, lat, lon, struttura)."""
    import netCDF4
    with netCDF4.Dataset("lfl.nc", memory=contenuto_nc) as ds:
        info = struttura(ds)
        lat, lon, t = trova(ds, "latitude"), trova(ds, "longitude"), trova(ds, "flash_time")
        if lat is None or lon is None or t is None:
            raise ValueError("Variabili latitude/longitude/flash_time non trovate")
        lat = np.ma.filled(lat[:].astype(float), np.nan)
        lon = np.ma.filled(lon[:].astype(float), np.nan)
        sec = np.ma.filled(t[:].astype(float), np.nan)
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(sec)
    tempi = [EPOCA + timedelta(seconds=float(s)) for s in sec[ok]]
    return tempi, lat[ok], lon[ok], info


def file_nc_del_prodotto(prodotto):
    """Restituisce i bytes dei file netCDF 'BODY' contenuti nel pacchetto."""
    voci = [e for e in prodotto.entries if e.endswith(".nc") and "BODY" in e]
    out = []
    if voci:
        for e in voci:
            with prodotto.open(entry=e) as f:
                out.append((e, f.read()))
        return out
    # ripiego: scarica lo zip intero
    with prodotto.open() as f:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
    for n in z.namelist():
        if n.endswith(".nc") and "BODY" in n:
            out.append((n, z.read(n)))
    return out


def main():
    import eumdac
    chiave = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    segreto = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if not chiave or not segreto:
        print("Segreti EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET mancanti.")
        sys.exit(1)
    try:
        token = eumdac.AccessToken((chiave, segreto))
        _ = str(token)  # forza la richiesta del token
    except Exception as e:
        print(f"1. Autenticazione NON riuscita: {type(e).__name__}. Controlla i due segreti.")
        sys.exit(1)
    print("1. Autenticazione riuscita.")

    ora = datetime.now(timezone.utc)
    dal_txt, al_txt = os.environ.get("DAL", "").strip(), os.environ.get("AL", "").strip()
    if dal_txt and al_txt:
        try:
            dal = datetime.strptime(dal_txt, "%Y-%m-%d %H:%M").replace(tzinfo=ROMA).astimezone(timezone.utc)
            al = datetime.strptime(al_txt, "%Y-%m-%d %H:%M").replace(tzinfo=ROMA).astimezone(timezone.utc)
        except ValueError:
            print('DAL e AL vanno scritti come "AAAA-MM-GG HH:MM" (ora italiana).')
            sys.exit(1)
        if not (timedelta(0) < al - dal <= timedelta(hours=6)):
            print("L'intervallo deve essere positivo e al massimo di 6 ore.")
            sys.exit(1)
        descr = f"dal {dal_txt} al {al_txt} (ora italiana, archivio)"
    else:
        dal, al = ora - timedelta(minutes=90), ora
        descr = "nell'ultima ora e mezza"
    store = eumdac.DataStore(token)
    coll = store.get_collection(COLLEZIONE)
    prodotti = list(coll.search(dtstart=dal, dtend=al))
    prodotti.sort(key=lambda p: p.sensing_start)
    print(f"\n2. Pacchetti LFL trovati {descr}: {len(prodotti)}")
    tutti_t, tutti_lat, tutti_lon = [], [], []
    stampata_struttura = False
    for p in prodotti:
        try:
            pubb = getattr(p, "ingested", None)
            rit = "" if not pubb else f", pubblicato {pubb.replace(tzinfo=timezone.utc).astimezone(ROMA):%H:%M:%S} " \
                f"({(pubb.replace(tzinfo=timezone.utc) - p.sensing_end.replace(tzinfo=timezone.utc)).total_seconds():.0f} s dopo la fine)"
            n_tot = 0
            for nome, dati in file_nc_del_prodotto(p):
                t, la, lo, info = leggi_flash(dati)
                n_tot += len(t)
                tutti_t += t
                tutti_lat.append(la)
                tutti_lon.append(lo)
                if not stampata_struttura:
                    print(f"   (struttura del file {nome.split('/')[-1][:60]}...)")
                    print("\n".join(info))
                    stampata_struttura = True
            s, e = p.sensing_start.replace(tzinfo=timezone.utc), p.sensing_end.replace(tzinfo=timezone.utc)
            print(f"   finestra {s.astimezone(ROMA):%H:%M}-{e.astimezone(ROMA):%H:%M}{rit}: {n_tot} flash sul disco intero")
        except Exception as e:
            print(f"   pacchetto {p}: lettura non riuscita ({type(e).__name__}: {str(e)[:120]})")

    if not tutti_t:
        print("\nNessun flash letto: controllare i messaggi sopra.")
        return
    lat = np.concatenate(tutti_lat)
    lon = np.concatenate(tutti_lon)
    # v2: solo i flash dentro l'intervallo richiesto (i pacchetti da 10 minuti possono sporgere)
    dentro = np.array([dal <= t < al for t in tutti_t])
    tutti_t = [t for t, k in zip(tutti_t, dentro) if k]
    lat, lon = lat[dentro], lon[dentro]
    if not tutti_t:
        print("\nNessun flash nell'intervallo richiesto.")
        return
    d, az = distanza_direzione(lat, lon)
    print(f"\n3. Flash per distanza dalla stazione ({dal.astimezone(ROMA):%d/%m %H:%M}-{al.astimezone(ROMA):%d/%m %H:%M} ora italiana):")
    prec = 0
    for f in FASCE:
        print(f"   {prec}-{f} km: {int(((d >= prec) & (d < f)).sum())}")
        prec = f
    lontani = (d >= 100) & (d < 300)
    if lontani.any():
        cont = {k: 0 for k in PUNTI}
        for a in az[lontani]:
            cont[PUNTI[int(((a % 360) + 22.5) // 45) % 8]] += 1
        print("   direzione dei flash tra 100 e 300 km: " + ", ".join(f"{k} {v}" for k, v in cont.items() if v))
    vicini = np.where(d < 100)[0]
    print(f"\n4. Flash entro 100 km: {len(vicini)} (in ordine di tempo, massimo 60)")
    passo = max(1, math.ceil(len(vicini) / 60))   # v2: il campione copre tutto l'intervallo
    if passo > 1:
        print(f"   (uno ogni {passo}, per coprire tutto l'intervallo)")
    for i in sorted(vicini, key=lambda k: tutti_t[k])[::passo][:60]:
        print(f"   {tutti_t[i].astimezone(ROMA):%d/%m %H:%M:%S}  {d[i]:5.1f} km  {PUNTI[int(((az[i] % 360) + 22.5) // 45) % 8]:>2}"
              f"  ({lat[i]:.3f}, {lon[i]:.3f})")
    tabella_5_minuti(tutti_t, d, az)
    print(f"\nLetto alle {ora.astimezone(ROMA):%H:%M:%S} ora italiana. Dati: EUMETSAT, MTG LI L2 (CC BY 4.0).")


def tabella_5_minuti(tempi, d, az):
    """v2: flash entro 40 km per intervallo di 5 minuti (ora italiana, inizio-fine)."""
    entro = np.where(d < 40)[0]
    print(f"\n5. Flash entro 40 km per intervallo di 5 minuti: {len(entro)} in totale")
    if not len(entro):
        return
    print("   intervallo     <20  20-30  30-40  totale   piu vicino")
    gruppi = {}
    for i in entro:
        t = tempi[i].astimezone(ROMA)
        inizio = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)
        gruppi.setdefault(inizio, []).append(i)
    for inizio in sorted(gruppi):
        ii = gruppi[inizio]
        dd = d[ii]
        k = ii[int(np.argmin(dd))]
        fine = inizio + timedelta(minutes=5)
        print(f"   {inizio:%d/%m %H:%M}-{fine:%H:%M}  {int((dd < 20).sum()):3d}  {int(((dd >= 20) & (dd < 30)).sum()):5d}"
              f"  {int((dd >= 30).sum()):5d}  {len(ii):6d}   {d[k]:4.1f} km {PUNTI[int(((az[k] % 360) + 22.5) // 45) % 8]}")


if __name__ == "__main__":
    main()
