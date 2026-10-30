"""Moto delle celle di pioggia e stima di arrivo sulla PGL320.

Sperimentale, NON allerta ufficiale. Metodo volutamente semplice e verificabile:
1. finestra di +-120 km attorno alla stazione sulle scansioni SRI (griglia 1 km);
2. vettore di spostamento d'insieme con la correlazione di fase (FFT) sul campo in
   scala logaritmica, su coppie di scansioni distanti 10 minuti; mediana dei
   vettori e loro dispersione come misura d'incertezza;
3. celle = aree connesse con almeno SOGLIA_CELLA mm/h e almeno MIN_AREA_KM2 km²;
   per ogni cella i pixel vengono proiettati lungo il moto: se qualche pixel passa
   entro RAGGIO_PASSAGGIO km dalla stazione, l'arrivo è il primo ingresso di un
   pixel nel cerchio di RAGGIO_PASSAGGIO km (stessa distanza del livello 1).
   L'arrivo è calcolato con la mediana e con ciascun vettore: l'intervallo e la
   quota di vettori che danno la cella in arrivo misurano l'incertezza.

Limiti: un solo vettore per tutta la finestra (niente rotazioni né moti diversi per
celle diverse), nessuna crescita o dissoluzione delle celle; le celle che nascono
sopra la stazione non danno preavviso.

Osservazione del 30/10/2026: nel mosaico SRI le scansioni a 5 minuti si alternano
(una cambia molto, la successiva poco); le coppie a 5 minuti danno spesso
spostamento nullo, quelle a 10 minuti vettori coerenti. Da qui PASSO_COPPIE = 2.
"""
import numpy as np
from rasterio.io import MemoryFile
from pyproj import Transformer
from scipy import ndimage

LAT, LON = 40.976, 17.13
MEZZA_FINESTRA_KM = 120
SOGLIA_CELLA = 0.5        # mm/h (scelta di Michele, 30/10/2026)
MIN_AREA_KM2 = 4          # celle più piccole ignorate (riduce gli echi isolati)
RAGGIO_PASSAGGIO = 5.0    # km: la cella "arriva" se passa entro questa distanza
RAGGIO_CELLE_KM = 60      # celle considerate
SPOST_MAX_PX = 15         # px per coppia (al massimo 90 km/h con coppie a 10 min)
MIN_FRAZIONE_PIOGGIA = 0.003
PASSO_COPPIE = 2          # coppie di scansioni distanti 10 minuti
MIN_VETTORI = 2
VELOCITA_MIN_KMH = 5.0

# Regole dell'avviso di avvicinamento (scelte di Michele, 30/10/2026)
ARRIVO_MIN_MIN = 5        # sotto, la pioggia è già praticamente sulla stazione
ARRIVO_MAX_MIN = 60
QUOTA_CONCORDI_MIN = 2 / 3

PUNTI = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest"]


def punto_cardinale(gradi):
    return PUNTI[int(((gradi % 360) + 22.5) // 45) % 8]


def ritaglia(raw):
    """GeoTIFF SRI grezzo -> (array mm/h con NaN dove manca il dato, riga, colonna
    della stazione nel ritaglio, georeferenziazione per la mappa)."""
    with MemoryFile(raw) as mem:
        with mem.open() as ds:
            if abs(ds.transform.a) != 1000 or abs(ds.transform.e) != 1000:
                raise ValueError("Griglia SRI diversa da 1 km")
            tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            x, y = tr.transform(LON, LAT)
            row, col = ds.index(x, y)
            h = MEZZA_FINESTRA_KM
            r0, c0 = row - h, col - h
            arr = ds.read(1, window=((r0, r0 + 2 * h + 1), (c0, c0 + 2 * h + 1)),
                          boundless=True, fill_value=-9999).astype(float)
            if ds.nodata is not None:
                arr[arr == ds.nodata] = np.nan
            arr[(arr < 0) | ~np.isfinite(arr)] = np.nan
            return arr, h, h, (ds.crs, ds.transform, r0, c0)


def _trasforma(a):
    """Scala logaritmica: 0 sotto 0,1 mm/h, poi log10(R)+1 (NaN -> 0)."""
    b = np.nan_to_num(a, nan=0.0)
    out = np.zeros_like(b)
    m = b >= 0.1
    out[m] = np.log10(b[m]) + 1.001
    return out


def spostamento(prima, dopo):
    """Spostamento in pixel (est, nord) da 'prima' a 'dopo'; None se poca pioggia."""
    f1, f2 = _trasforma(prima), _trasforma(dopo)
    if min((f1 > 0).mean(), (f2 > 0).mean()) < MIN_FRAZIONE_PIOGGIA:
        return None
    win = np.outer(np.hanning(f1.shape[0]), np.hanning(f1.shape[1]))
    F1, F2 = np.fft.fft2(f1 * win), np.fft.fft2(f2 * win)
    R = F2 * np.conj(F1)
    R /= np.abs(R) + 1e-9
    c = np.fft.fftshift(np.real(np.fft.ifft2(R)))
    cy, cx = np.array(c.shape) // 2
    s = SPOST_MAX_PX
    sub = c[cy - s:cy + s + 1, cx - s:cx + s + 1]
    iy, ix = np.unravel_index(np.argmax(sub), sub.shape)

    def parabola(m1, m0, p1):
        d = m1 - 2 * m0 + p1
        return 0.0 if d == 0 else 0.5 * (m1 - p1) / d
    dy = iy - s + (parabola(sub[iy - 1, ix], sub[iy, ix], sub[iy + 1, ix]) if 0 < iy < 2 * s else 0)
    dx = ix - s + (parabola(sub[iy, ix - 1], sub[iy, ix], sub[iy, ix + 1]) if 0 < ix < 2 * s else 0)
    return float(dx), float(-dy)  # righe crescenti verso sud: nord = -dy


def stima_moto(campi, passo=PASSO_COPPIE):
    """campi: array consecutivi a 5 minuti, dal più vecchio (None se mancante).

    Restituisce dict o None se le coppie utili sono meno di MIN_VETTORI.
    """
    coppie = [(a, b) for a, b in zip(campi, campi[passo:]) if a is not None and b is not None]
    vett = [v for v in (spostamento(a, b) for a, b in coppie) if v]
    if len(vett) < MIN_VETTORI:
        return None
    v = np.array(vett) * 12.0 / passo  # km/h
    med = np.median(v, axis=0)
    verso = (np.degrees(np.arctan2(med[0], med[1])) + 360) % 360
    return {"vettori_kmh": v.tolist(), "est_kmh": float(med[0]), "nord_kmh": float(med[1]),
            "velocita_kmh": float(np.hypot(*med)), "va_verso_gradi": float(verso),
            "provenienza_gradi": float((verso + 180) % 360),
            "dispersione_kmh": float(np.max(np.hypot(v[:, 0] - med[0], v[:, 1] - med[1])))}


def _arrivo(est_m, nord_m, dmin, ve, vn):
    """Minuti all'arrivo per un vettore (None se la cella non passa sulla stazione)."""
    vv = np.hypot(ve, vn)
    if vv < VELOCITA_MIN_KMH:
        return None
    ux, uy = ve / vv, vn / vv
    lungo = (-est_m) * ux + (-nord_m) * uy           # km da percorrere lungo il moto
    trasv = np.abs((-est_m) * uy - (-nord_m) * ux)   # distanza dalla traiettoria
    if dmin <= RAGGIO_PASSAGGIO:
        return 0.0
    ok = (lungo > 0) & (trasv <= RAGGIO_PASSAGGIO)
    if not ok.any():
        return None
    # tempo per ENTRARE nel cerchio di RAGGIO_PASSAGGIO km (come il livello 1),
    # non per arrivare sul punto più vicino alla stazione
    ingresso = lungo[ok] - np.sqrt(RAGGIO_PASSAGGIO ** 2 - trasv[ok] ** 2)
    return float(max(0.0, ingresso.min()) / vv * 60)


def celle(campo, r_st, c_st, moto):
    """Celle entro RAGGIO_CELLE_KM con distanza, direzione e arrivo stimato."""
    b = np.nan_to_num(campo, nan=0.0)
    lab, n = ndimage.label(b >= SOGLIA_CELLA)
    rr, cc = np.indices(b.shape)
    est = cc - c_st
    nord = r_st - rr
    dist = np.hypot(est, nord)
    out = []
    for k in range(1, n + 1):
        m = lab == k
        area = int(m.sum())
        if area < MIN_AREA_KM2:
            continue
        dmin = float(dist[m].min())
        if dmin > RAGGIO_CELLE_KM:
            continue
        i = int(np.argmin(np.where(m, dist, np.inf)))
        az = (np.degrees(np.arctan2(est.flat[i], nord.flat[i])) + 360) % 360
        c = {"area_km2": area, "max_mm_h": float(b[m].max()), "dist_km": dmin,
             "direzione_gradi": float(az), "punto_km": (float(est.flat[i]), float(nord.flat[i])),
             "arrivo_min": None, "arrivo_range_min": None, "quota_concordi": 0.0}
        if moto:
            principale = _arrivo(est[m], nord[m], dmin, moto["est_kmh"], moto["nord_kmh"])
            singoli = [_arrivo(est[m], nord[m], dmin, ve, vn) for ve, vn in moto["vettori_kmh"]]
            validi = [e for e in singoli if e is not None]
            c["quota_concordi"] = len(validi) / len(singoli) if singoli else 0.0
            if principale is not None:
                tutti = validi + [principale]
                c["arrivo_min"] = principale
                c["arrivo_range_min"] = (min(tutti), max(tutti))
        out.append(c)
    return sorted(out, key=lambda c: c["dist_km"])


def cella_in_avvicinamento(lista):
    """La cella che soddisfa le regole dell'avviso (arrivo più vicino), o None."""
    buone = [c for c in lista
             if c["arrivo_min"] is not None
             and ARRIVO_MIN_MIN <= c["arrivo_min"] <= ARRIVO_MAX_MIN
             and c["quota_concordi"] >= QUOTA_CONCORDI_MIN - 1e-9]
    return min(buone, key=lambda c: c["arrivo_min"]) if buone else None


def descrivi_moto(moto):
    if not moto or moto["velocita_kmh"] < VELOCITA_MIN_KMH:
        return "moto non stimabile"
    return (f"in moto da {punto_cardinale(moto['provenienza_gradi'])} "
            f"a {moto['velocita_kmh']:.0f} km/h")
