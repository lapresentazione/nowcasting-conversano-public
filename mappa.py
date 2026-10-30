"""Mappa radar di +-50 km attorno alla PGL320 da allegare agli avvisi Telegram.

Mostra la pioggia stimata (SRI), i cerchi a 5/10/20/40 km, la costa (Natural Earth,
pubblico dominio), alcune località di riferimento, la freccia del moto e le celle
che soddisfano le regole dell'avviso di avvicinamento con l'arrivo stimato.
"""
import io
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np
from pyproj import Transformer

QUI = os.path.dirname(os.path.abspath(__file__))
SOGLIE = [0.1, 1, 2, 5, 10, 20, 50, 500]
COLORI = ["#c6dbef", "#6baed6", "#2171b5", "#41ab5d", "#fec44f", "#e31a1c", "#7a0177"]
# Località di riferimento per orientarsi (coordinate approssimate al centesimo di grado).
LOCALITA = {"Bari": (41.12, 16.87), "Mola": (41.06, 17.09), "Polignano": (41.00, 17.22),
            "Monopoli": (40.95, 17.30), "Castellana": (40.89, 17.17),
            "Putignano": (40.85, 17.12), "Gioia d. C.": (40.80, 16.92)}


def mostra_cella(c):
    """Sulla mappa si etichettano solo le celle che rispettano le regole dell'avviso."""
    from movimento import ARRIVO_MIN_MIN, ARRIVO_MAX_MIN, QUOTA_CONCORDI_MIN
    return (c.get("arrivo_min") is not None and ARRIVO_MIN_MIN <= c["arrivo_min"] <= ARRIVO_MAX_MIN
            and c.get("quota_concordi", 0) >= QUOTA_CONCORDI_MIN - 1e-9)


def _a_km(geo, r_st, c_st, lon, lat):
    """lon/lat -> km (est, nord) rispetto alla stazione, nella griglia del ritaglio."""
    crs, transform, r0, c0 = geo
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = tr.transform(np.asarray(lon), np.asarray(lat))
    col = (np.asarray(x) - transform.c) / transform.a   # griglia senza rotazione
    row = (np.asarray(y) - transform.f) / transform.e
    return (col - c0 - 0.5) - c_st, r_st - (row - r0 - 0.5)


def disegna(campo, r_st, c_st, geo, titolo, moto=None, celle=(), raggio=50):
    """Restituisce i byte PNG della mappa."""
    h = raggio
    sub = campo[r_st - h:r_st + h + 1, c_st - h:c_st + h + 1]
    fig, ax = plt.subplots(figsize=(6.4, 6.4), dpi=110)
    ax.set_facecolor("#f4f4f4")
    nodata = np.where(np.isnan(sub), 1.0, np.nan)
    ax.imshow(nodata, extent=(-h - .5, h + .5, -h - .5, h + .5), cmap="Greys", vmin=0, vmax=4,
              interpolation="nearest")
    pioggia = np.where(np.nan_to_num(sub) >= 0.1, sub, np.nan)
    norm = BoundaryNorm(SOGLIE, len(COLORI))
    im = ax.imshow(pioggia, extent=(-h - .5, h + .5, -h - .5, h + .5), cmap=ListedColormap(COLORI),
                   norm=norm, interpolation="nearest")
    with open(os.path.join(QUI, "costa_puglia.json")) as fh:
        for tratto in json.load(fh):
            lon, lat = zip(*tratto)
            x, y = _a_km(geo, r_st, c_st, lon, lat)
            ax.plot(x, y, color="#555555", lw=0.8)
    for nome, (lat, lon) in LOCALITA.items():
        x, y = _a_km(geo, r_st, c_st, [lon], [lat])
        ax.plot(x, y, "s", ms=2.5, color="#333333")
        ax.annotate(nome, (x[0], y[0]), xytext=(3, 2) if x[0] < 30 else (-40, 4), textcoords="offset points", fontsize=7,
                    color="#333333")
    for r in (5, 10, 20, 40):
        ax.add_patch(plt.Circle((0, 0), r, fill=False, ls="--", lw=0.7, color="#444444"))
        ax.text(r * 0.707, r * 0.707, f"{r}", fontsize=7, color="#444444")
    ax.plot(0, 0, marker="*", ms=13, color="black", mec="white")
    if moto and moto["velocita_kmh"] >= 5:
        # freccia: spostamento in 60 minuti, centrata nel riquadro in basso a sinistra
        dx, dy = moto["est_kmh"], moto["nord_kmh"]
        cx0, cy0 = -h + 18, -h + 18
        ax.annotate("", xy=(cx0 + dx / 2, cy0 + dy / 2), xytext=(cx0 - dx / 2, cy0 - dy / 2),
                    arrowprops=dict(arrowstyle="-|>", lw=2, color="black"))
        ax.text(-h + 2, -h + 2, f"moto {moto['velocita_kmh']:.0f} km/h (freccia = 1 ora)",
                fontsize=7, bbox=dict(fc="white", alpha=0.7, lw=0))
    for c in celle:
        if not mostra_cella(c):
            continue
        x, y = c["punto_km"]
        lo, hi = c["arrivo_range_min"]
        ax.plot(x, y, "o", ms=6, mfc="none", mec="black", mew=1.5)
        ax.annotate(f"arrivo ~{c['arrivo_min']:.0f} min\n({lo:.0f}-{hi:.0f})", (x, y),
                    xytext=(-60 if x > 15 else 6, -22 if y > -35 else 8),
                    textcoords="offset points", fontsize=8, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", alpha=0.85))
    ax.set_xlim(-h, h)
    ax.set_ylim(-h, h)
    ax.set_xlabel("km verso est")
    ax.set_ylabel("km verso nord")
    ax.set_title(titolo, fontsize=9)
    cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02, ticks=SOGLIE[:-1])
    cb.ax.set_yticklabels(["0,1", "1", "2", "5", "10", "20", "50"])
    cb.set_label("mm/h stimati", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    fig.text(0.01, 0.005, "Dati: radar Protezione Civile (SRI). Stima sperimentale, non ufficiale. "
             "Grigio: nessun dato.", fontsize=6, color="#555555")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
