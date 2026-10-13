"""Build a branch-specific price snapshot from retailers' official price feeds.

A bounded pilot is intentional: publishing all Israeli branch SKUs to GitHub Pages
would produce an excessively large repository. Extend CHAIN_LIMITS and CITY_FILTER
after validating the workflow. Missing sources are never replaced with mock prices.
"""
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

import israeli_prices as ilp

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CITY_FILTER = [x.strip() for x in os.getenv("ZOL_CITIES", "כפר סבא,הוד השרון,רעננה").split(",") if x.strip()]
CHAINS = [x.strip() for x in os.getenv("ZOL_CHAINS", "shufersal,rami-levy,super-yuda,tiv-taam,victory,carrefour").split(",") if x.strip()]
MAX_STORES_PER_CHAIN = int(os.getenv("ZOL_MAX_STORES_PER_CHAIN", "12"))
MAX_EXPORT_ROWS = int(os.getenv("ZOL_MAX_EXPORT_ROWS", "150000"))
now = dt.datetime.now(dt.timezone.utc).isoformat()
status = {"generatedAt": now, "cities": CITY_FILTER, "chains": {}, "successStores": 0, "failures": []}

def value(o, *names, default=None):
    if isinstance(o, dict):
        return next((o[n] for n in names if o.get(n) is not None), default)
    return next((getattr(o, n) for n in names if getattr(o, n, None) is not None), default)

def entries(o, *names):
    if isinstance(o, (list, tuple)):
        return o
    return value(o, *names, default=[]) or []

def text(o):
    return str(o or "").strip()

dbfile = pathlib.Path(tempfile.gettempdir()) / "zolharetz_prices.sqlite"
db = sqlite3.connect(dbfile)
db.executescript("""
CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY, chain TEXT, name TEXT, city TEXT, address TEXT);
CREATE TABLE IF NOT EXISTS prices(store_id TEXT, barcode TEXT, name TEXT, price REAL, updated TEXT, PRIMARY KEY(store_id,barcode));
""")
for chain in CHAINS:
    status["chains"][chain] = {"stores": 0, "items": 0}
    try:
        response = ilp.get_stores(chain)
        candidates = entries(response, "stores", "items")
        selected = [s for s in candidates if any(c in text(value(s, "city", "city_name", "cityName")) for c in CITY_FILTER)]
        if not selected:
            status["failures"].append(chain + ": no branches matched configured cities")
        for s in selected[:MAX_STORES_PER_CHAIN]:
            sid = text(value(s, "store_id", "storeId", "id"))
            if not sid:
                continue
            key = chain + ":" + sid
            try:
                pf = ilp.get_prices(chain, store_id=sid)
                items = entries(pf, "items", "products")
                rows = []
                for item in items:
                    barcode = text(value(item, "item_code", "itemCode", "barcode"))
                    name = text(value(item, "item_name", "itemName", "name"))
                    raw = value(item, "price", "item_price", "itemPrice")
                    try:
                        price = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if not barcode or not name or price < 0:
                        continue
                    updated = text(value(item, "price_update_date", "priceUpdateDate")) or now
                    rows.append((key, barcode, name, price, updated))
                if not rows:
                    status["failures"].append(key + ": no valid product prices")
                    continue
                db.execute("INSERT OR REPLACE INTO stores VALUES(?,?,?,?,?)", (key, chain, text(value(s,"store_name","storeName","name")), text(value(s,"city","city_name","cityName")), text(value(s,"address","street"))))
                db.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?)", rows)
                db.commit()
                status["chains"][chain]["stores"] += 1
                status["chains"][chain]["items"] += len(rows)
                status["successStores"] += 1
                print(key, len(rows), flush=True)
            except Exception as exc:
                status["failures"].append(key + ": " + str(exc)[:180])
    except Exception as exc:
        status["failures"].append(chain + ": " + str(exc)[:180])

# Never replace a previously good snapshot with empty results.
if status["successStores"]:
    stores = [dict(zip(["id","chain","name","city","address"], row)) for row in db.execute("SELECT * FROM stores")]
    rows = [list(row) for row in db.execute("SELECT store_id,barcode,name,price,updated FROM prices ORDER BY store_id,barcode LIMIT ?", (MAX_EXPORT_ROWS,))]
    snapshot = {"generatedAt": now, "source": "Official retailer price-transparency feeds via israeli-prices", "stores": stores, "prices": rows, "truncated": db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] > len(rows)}
    tmp = DATA / "snapshot.tmp"
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",",":")), encoding="utf-8")
    tmp.replace(DATA / "snapshot.json")
(DATA / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(status, ensure_ascii=False))
if not status["successStores"]:
    print("No stores downloaded; preserving existing snapshot", file=sys.stderr)
    sys.exit(1)
