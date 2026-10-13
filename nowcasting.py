"""Prototipo SRI per Conversano PGL320. Sperimentale, NON allerta ufficiale."""
import argparse, json, os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.windows import Window
from pyproj import Transformer
import requests

LAT, LON = 40.976, 17.13
API = "https://radar-api.protezionecivile.it"
HEADERS = {"Origin": "https://radar.protezionecivile.it", "User-Agent": "NowcastingConversano/0.2"}
ITALY = ZoneInfo("Europe/Rome")
STEP = 300_000

def observe(raw, when):
    with MemoryFile(raw) as mem:
        with mem.open() as ds:
            if ds.count != 1 or ds.crs is None or ds.crs.is_geographic:
                raise ValueError("Raster SRI non georeferenziato in coordinate metriche")
            tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            x,y = tr.transform(LON,LAT)
            row,col = ds.index(x,y)
            if not (0<=row<ds.height and 0<=col<ds.width):
                raise ValueError("PGL320 fuori dalla griglia")
            px,py = abs(ds.transform.a),abs(ds.transform.e)
            if not (100<=px<=2000 and 100<=py<=2000):
                raise ValueError("Dimensione celle inattesa")
            w = Window(max(0,col-45),max(0,row-45),0,0)
            w = Window(w.col_off,w.row_off,min(ds.width-w.col_off,91),min(ds.height-w.row_off,91))
            arr=ds.read(1,window=w)
            rr,cc=np.indices(arr.shape)
            cc=cc+w.col_off+0.5; rr=rr+w.row_off+0.5
            xx=ds.transform.c+cc*ds.transform.a+rr*ds.transform.b
            yy=ds.transform.f+cc*ds.transform.d+rr*ds.transform.e
            dist=np.hypot(xx-x, yy-y)/1000
            valid=np.isfinite(arr)&(arr>=0)&(arr!=-9999)
            if ds.nodata is not None: valid &= arr!=ds.nodata
            point=float(arr[row-int(w.row_off),col-int(w.col_off)]) if valid[row-int(w.row_off),col-int(w.col_off)] else None
            stats={}
            for radius in (5,10,20,40):
                area=dist<=radius
                ok=area&valid
                stats[str(radius)]={
                    "coverage":float(ok.sum()/area.sum()) if area.any() else 0,
                    "max_mm_h":float(arr[ok].max()) if ok.any() else None,
                    "cells_ge_1":int((ok&(arr>=1)).sum()),
                    "cells_ge_5":int((ok&(arr>=5)).sum()),
                    "cells_ge_10":int((ok&(arr>=10)).sum())
                }
            return {"when":when,"point":point,"stats":stats}

def evaluate(a,b):
    if b["when"]-a["when"] != STEP: return 0,"scansioni non consecutive"
    if any(o["point"] is None or o["stats"]["10"]["coverage"]<0.90 for o in (a,b)):
        return 0,"copertura o cella locale insufficiente"
    def near(o): return o["stats"]["5"]["cells_ge_1"]>=2
    def heavy(o): return o["stats"]["10"]["cells_ge_5"]>=3 and o["stats"]["10"]["cells_ge_10"]>=1
    if heavy(a) and heavy(b): return 2,"precipitazioni intense entro 10 km"
    if near(a) and near(b): return 1,"pioggia entro 5 km"
    return 0,"sotto soglia"

def get_scans():
    s=requests.Session(); s.headers.update(HEADERS)
    r=s.get(API+"/findLastProductByType",params={"type":"SRI"},timeout=20);r.raise_for_status()
    items=r.json().get("lastProducts") or []
    if not items or items[0].get("productType")!="SRI": raise ValueError("SRI non disponibile")
    t=int(items[0]["time"])
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    if not (-120_000 <= now-t <= 25*60_000):raise ValueError("Scansione obsoleta o futura")
    result=[]
    for ts in (t-STEP,t):
        r=s.post(API+"/downloadProduct",json={"productType":"SRI","productDate":ts},timeout=20)
        r.raise_for_status();url=r.json().get("url","")
        from urllib.parse import urlsplit
        u=urlsplit(url)
        if u.scheme!="https" or not (u.hostname or "").endswith(".s3.eu-south-1.amazonaws.com"):
            raise ValueError("Host di download non autorizzato")
        rr=s.get(url,timeout=45);rr.raise_for_status()
        if not (1024<=len(rr.content)<=50_000_000):raise ValueError("Dimensione raster inattesa")
        result.append(observe(rr.content,ts))
    return result

def message(o, level,reason):
    t=datetime.fromtimestamp(o["when"]/1000,ITALY).strftime("%d/%m/%Y %H:%M %Z")
    r=10 if level==2 else 5
    s=o["stats"][str(r)]
    point="non disponibile" if o["point"] is None else f'{o["point"]:.2f} mm/h'
    return (f"Radar Conversano: {reason}\nScansione: {t}\n"
            f"Massimo entro {r} km: {s['max_mm_h']:.2f} mm/h. "
            f"Stima cella PGL320: {point}.\n"
            "Precipitazione stimata, non misura al suolo. Non implica un temporale "
            "né consente di prevedere il movimento. Avviso sperimentale non ufficiale.\n"
            "Radar Protezione Civile: https://mappe.protezionecivile.gov.it/it/mappe-rischi/piattaforma-radar/")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--notify",action="store_true")
    p.add_argument("--state",default="stato_nowcasting.json")
    p.add_argument("--test",action="store_true",help="test API e lettura senza invio e senza stato")
    args=p.parse_args()
    a,b=get_scans()
    lvl,reason=evaluate(a,b)
    print(datetime.fromtimestamp(b["when"]/1000,ITALY), "PGL320",b["point"],"livello",lvl,reason)
    if args.test: return
    statepath=Path(args.state)
    st=json.loads(statepath.read_text()) if statepath.exists() else {"last":0,"active":False,"level":0,"quiet":0}
    if b["when"] <= st["last"]:return
    # Una coppia con dati inaffidabili non modifica lo stato dell'episodio.
    if reason in ("scansioni non consecutive","copertura o cella locale insufficiente"):return
    evt=lvl>0 and (not st["active"] or lvl>st["level"])
    if evt:
        msg=message(b,lvl,reason)
        if args.notify:
            token=os.environ.get("TELEGRAM_BOT_TOKEN")
            chat=os.environ.get("TELEGRAM_CHAT_ID")
            if not token or not chat:raise RuntimeError("Mancano i segreti Telegram: nessun invio")
            res=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id":chat,"text":msg,"disable_web_page_preview":True},timeout=20)
            res.raise_for_status()
            if not res.json().get("ok"):raise RuntimeError("Telegram non conferma invio")
        else:
            print("SOLO SIMULAZIONE:\n"+msg)
    if lvl:
        st.update(active=True,level=max(lvl,st["level"]),quiet=0)
    else:
        st["quiet"]+=1
        if st["quiet"]>=2:st.update(active=False,level=0,quiet=0)
    st["last"]=b["when"]
    statepath.write_text(json.dumps(st,indent=2))
if __name__=="__main__":
    main()
