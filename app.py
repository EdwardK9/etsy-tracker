import csv
import io
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/etsy.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

app = Flask(__name__, static_folder="static")

DEFAULT_SETTINGS = {
    "currency": "£",
    "bambuddy_url": "",
    "bambuddy_key": "",
    "default_cost_per_kg": "18",
    # Etsy fee defaults (UK). Edit these to match your own Etsy statement.
    "fee_transaction_pct": "6.5",
    "fee_processing_pct": "4",
    "fee_processing_fixed": "0.20",
    "fee_regulatory_pct": "0.32",
    "fee_listing_per_item": "0.15",
    "fee_vat_pct": "0",
    "fee_offsite_ads_pct": "15",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sku TEXT DEFAULT '',
  price REAL DEFAULT 0,
  archive_id INTEGER,
  archive_name TEXT DEFAULT '',
  plate_grams REAL DEFAULT 0,
  plate_cost REAL DEFAULT 0,
  items_per_plate INTEGER DEFAULT 1,
  extra_cost REAL DEFAULT 0,
  print_minutes INTEGER DEFAULT 0,
  reorder_at INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  value REAL NOT NULL,
  changed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  qty_good INTEGER DEFAULT 0,
  qty_failed INTEGER DEFAULT 0,
  unit_cost REAL DEFAULT 0,
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  order_ref TEXT DEFAULT '',
  product_id INTEGER NOT NULL,
  qty INTEGER DEFAULT 1,
  unit_price REAL DEFAULT 0,
  shipping_charged REAL DEFAULT 0,
  discount REAL DEFAULT 0,
  postage_cost REAL DEFAULT 0,
  offsite_ad INTEGER DEFAULT 0,
  fees REAL DEFAULT 0,
  unit_cost REAL DEFAULT 0,
  source TEXT DEFAULT 'manual',
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  category TEXT DEFAULT 'Other',
  description TEXT DEFAULT '',
  amount REAL DEFAULT 0
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()


def rows(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def settings():
    return {r["key"]: r["value"] for r in db().execute("SELECT key,value FROM settings")}


def num(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace("£", "").replace("$", "").replace("€", "").replace(",", "").strip())
    except ValueError:
        return default


def today():
    return datetime.now().strftime("%Y-%m-%d")


def unit_cost_of(p):
    per_plate = num(p.get("plate_cost"))
    ipp = max(int(num(p.get("items_per_plate"), 1)), 1)
    return round(per_plate / ipp + num(p.get("extra_cost")), 4)


def calc_fees(gross, qty, offsite, s):
    f = gross * (num(s["fee_transaction_pct"]) + num(s["fee_regulatory_pct"]) + num(s["fee_processing_pct"])) / 100
    f += num(s["fee_processing_fixed"]) + num(s["fee_listing_per_item"]) * qty
    if offsite:
        f += gross * num(s["fee_offsite_ads_pct"]) / 100
    f *= 1 + num(s["fee_vat_pct"]) / 100
    return round(f, 2)


# ---------------------------------------------------------------- static
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- settings
@app.route("/api/settings", methods=["GET", "PUT"])
def api_settings():
    if request.method == "PUT":
        data = request.get_json(force=True)
        for k, v in data.items():
            if k == "bambuddy_key" and not v:
                continue  # blank = keep existing key
            if k in DEFAULT_SETTINGS:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
        db().commit()
    s = settings()
    s["bambuddy_key_set"] = bool(s.get("bambuddy_key"))
    s["bambuddy_key"] = ""  # never send the key back to the browser
    return jsonify(s)


# ---------------------------------------------------------------- products
def product_rows():
    ps = rows("""
      SELECT p.*,
        COALESCE((SELECT SUM(qty_good) FROM batches b WHERE b.product_id=p.id),0) AS made,
        COALESCE((SELECT SUM(qty_failed) FROM batches b WHERE b.product_id=p.id),0) AS failed,
        COALESCE((SELECT SUM(qty) FROM sales s WHERE s.product_id=p.id),0) AS sold
      FROM products p ORDER BY p.active DESC, p.name COLLATE NOCASE""")
    for p in ps:
        p["stock"] = p["made"] - p["sold"]
        p["unit_cost"] = unit_cost_of(p)
        p["margin_est"] = round(p["price"] - p["unit_cost"], 2)
    return ps


@app.route("/api/products", methods=["GET", "POST"])
def api_products():
    if request.method == "POST":
        d = request.get_json(force=True)
        cur = db().execute(
            """INSERT INTO products(name,sku,price,archive_id,archive_name,plate_grams,plate_cost,
               items_per_plate,extra_cost,print_minutes,reorder_at,active,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("name", "").strip() or "Untitled", d.get("sku", ""), num(d.get("price")),
             d.get("archive_id") or None, d.get("archive_name", ""), num(d.get("plate_grams")),
             num(d.get("plate_cost")), max(int(num(d.get("items_per_plate"), 1)), 1),
             num(d.get("extra_cost")), int(num(d.get("print_minutes"))), int(num(d.get("reorder_at"))),
             1 if d.get("active", 1) else 0, d.get("notes", "")))
        pid = cur.lastrowid
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (pid, "price", num(d.get("price"))))
        p = one("SELECT * FROM products WHERE id=?", (pid,))
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (pid, "cost", unit_cost_of(p)))
        db().commit()
        return jsonify({"id": pid})
    return jsonify(product_rows())


@app.route("/api/products/<int:pid>", methods=["PUT", "DELETE"])
def api_product(pid):
    old = one("SELECT * FROM products WHERE id=?", (pid,))
    if not old:
        return jsonify({"error": "not found"}), 404
    if request.method == "DELETE":
        if one("SELECT 1 AS x FROM sales WHERE product_id=? LIMIT 1", (pid,)) or \
           one("SELECT 1 AS x FROM batches WHERE product_id=? LIMIT 1", (pid,)):
            return jsonify({"error": "Product has sales/batches. Mark it inactive instead."}), 400
        db().execute("DELETE FROM history WHERE product_id=?", (pid,))
        db().execute("DELETE FROM products WHERE id=?", (pid,))
        db().commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    new = dict(old)
    for k in ("name", "sku", "archive_name", "notes"):
        if k in d:
            new[k] = d[k]
    for k in ("price", "plate_grams", "plate_cost", "extra_cost"):
        if k in d:
            new[k] = num(d[k])
    for k in ("items_per_plate", "print_minutes", "reorder_at"):
        if k in d:
            new[k] = max(int(num(d[k])), 1 if k == "items_per_plate" else 0)
    if "archive_id" in d:
        new["archive_id"] = d["archive_id"] or None
    if "active" in d:
        new["active"] = 1 if d["active"] else 0
    save_product(new, old)
    return jsonify({"ok": True})


def save_product(new, old):
    db().execute(
        """UPDATE products SET name=?,sku=?,price=?,archive_id=?,archive_name=?,plate_grams=?,plate_cost=?,
           items_per_plate=?,extra_cost=?,print_minutes=?,reorder_at=?,active=?,notes=? WHERE id=?""",
        (new["name"], new["sku"], new["price"], new["archive_id"], new["archive_name"], new["plate_grams"],
         new["plate_cost"], new["items_per_plate"], new["extra_cost"], new["print_minutes"], new["reorder_at"],
         new["active"], new["notes"], new["id"]))
    if abs(num(new["price"]) - num(old["price"])) > 1e-9:
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (new["id"], "price", new["price"]))
    if abs(unit_cost_of(new) - unit_cost_of(old)) > 1e-9:
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (new["id"], "cost", unit_cost_of(new)))
    db().commit()


@app.route("/api/products/<int:pid>/history")
def api_history(pid):
    return jsonify(rows("SELECT kind,value,changed_at FROM history WHERE product_id=? ORDER BY changed_at,id", (pid,)))


# ---------------------------------------------------------------- bambuddy
class BBError(Exception):
    pass


def bb_get(path, params=None):
    s = settings()
    base = (s.get("bambuddy_url") or "").strip().rstrip("/")
    if not base:
        raise BBError("Bambuddy URL not set (Settings tab).")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-API-Key": s.get("bambuddy_key", ""), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BBError(f"Bambuddy returned HTTP {e.code} for {path}")
    except Exception as e:
        raise BBError(f"Could not reach Bambuddy: {e}")


def norm_archive(a, s):
    grams = num(a.get("filament_used_grams"))
    cost = a.get("cost")
    est = False
    if cost in (None, "", 0):
        cost = grams / 1000.0 * num(s.get("default_cost_per_kg"))
        est = True
    return {
        "id": a.get("id"),
        "name": a.get("print_name") or a.get("filename") or f"Archive {a.get('id')}",
        "grams": round(grams, 2),
        "cost": round(num(cost), 4),
        "cost_estimated": est,
        "status": a.get("status", ""),
        "minutes": int(num(a.get("print_time_seconds")) // 60),
        "type": a.get("filament_type", ""),
    }


def as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "archives", "results", "data"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


@app.route("/api/bambuddy/archives")
def api_bb_archives():
    q = request.args.get("q", "").strip()
    s = settings()
    try:
        if len(q) >= 2:
            data = bb_get("/archives/search", {"q": q, "limit": 50})
        else:
            data = bb_get("/archives", {"limit": 50})
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify([norm_archive(a, s) for a in as_list(data)])


@app.route("/api/bambuddy/test")
def api_bb_test():
    try:
        a = as_list(bb_get("/archives", {"limit": 1}))
        try:
            sp = as_list(bb_get("/inventory/spools"))
            spools = len(sp)
        except BBError:
            spools = None
        return jsonify({"ok": True, "archives_ok": True, "spools": spools})
    except BBError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/products/<int:pid>/refresh-bambuddy", methods=["POST"])
def api_refresh_bb(pid):
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p or not p["archive_id"]:
        return jsonify({"error": "Product is not linked to a Bambuddy archive."}), 400
    try:
        a = bb_get(f"/archives/{p['archive_id']}")
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    n = norm_archive(a, settings())
    new = dict(p)
    new.update(plate_grams=n["grams"], plate_cost=n["cost"], archive_name=n["name"])
    if n["minutes"]:
        new["print_minutes"] = n["minutes"] // max(p["items_per_plate"], 1)
    save_product(new, p)
    return jsonify({"ok": True, **n})


# ---------------------------------------------------------------- batches
@app.route("/api/batches", methods=["GET", "POST"])
def api_batches():
    if request.method == "POST":
        d = request.get_json(force=True)
        p = one("SELECT * FROM products WHERE id=?", (d.get("product_id"),))
        if not p:
            return jsonify({"error": "Pick a product."}), 400
        uc = num(d.get("unit_cost"), None) if d.get("unit_cost") not in (None, "") else unit_cost_of(p)
        db().execute("INSERT INTO batches(product_id,date,qty_good,qty_failed,unit_cost,notes) VALUES(?,?,?,?,?,?)",
                     (p["id"], d.get("date") or today(), int(num(d.get("qty_good"))), int(num(d.get("qty_failed"))),
                      uc, d.get("notes", "")))
        db().commit()
        return jsonify({"ok": True})
    return jsonify(rows("""SELECT b.*, p.name AS product FROM batches b JOIN products p ON p.id=b.product_id
                           ORDER BY b.date DESC, b.id DESC"""))


@app.route("/api/batches/<int:bid>", methods=["DELETE"])
def api_batch(bid):
    db().execute("DELETE FROM batches WHERE id=?", (bid,))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- sales
def sale_values(d, p, s):
    qty = max(int(num(d.get("qty"), 1)), 1)
    price = num(d.get("unit_price"), None) if d.get("unit_price") not in (None, "") else p["price"]
    ship = num(d.get("shipping_charged"))
    disc = num(d.get("discount"))
    offsite = 1 if d.get("offsite_ad") else 0
    gross = max(price * qty + ship - disc, 0)
    if d.get("fees") in (None, ""):
        fees = calc_fees(gross, qty, offsite, s)
    else:
        fees = num(d.get("fees"))
    return qty, price, ship, disc, offsite, fees


@app.route("/api/sales", methods=["GET", "POST"])
def api_sales():
    if request.method == "POST":
        d = request.get_json(force=True)
        p = one("SELECT * FROM products WHERE id=?", (d.get("product_id"),))
        if not p:
            return jsonify({"error": "Pick a product."}), 400
        qty, price, ship, disc, offsite, fees = sale_values(d, p, settings())
        db().execute(
            """INSERT INTO sales(date,order_ref,product_id,qty,unit_price,shipping_charged,discount,postage_cost,
               offsite_ad,fees,unit_cost,source,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("date") or today(), d.get("order_ref", ""), p["id"], qty, price, ship, disc,
             num(d.get("postage_cost")), offsite, fees, unit_cost_of(p), "manual", d.get("notes", "")))
        db().commit()
        return jsonify({"ok": True})
    return jsonify(rows("""SELECT s.*, p.name AS product,
        (s.unit_price*s.qty + s.shipping_charged - s.discount) AS revenue,
        (s.unit_price*s.qty + s.shipping_charged - s.discount - s.fees - s.postage_cost - s.unit_cost*s.qty) AS profit
        FROM sales s JOIN products p ON p.id=s.product_id ORDER BY s.date DESC, s.id DESC"""))


@app.route("/api/sales/<int:sid>", methods=["PUT", "DELETE"])
def api_sale(sid):
    if request.method == "DELETE":
        db().execute("DELETE FROM sales WHERE id=?", (sid,))
        db().commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    old = one("SELECT * FROM sales WHERE id=?", (sid,))
    if not old:
        return jsonify({"error": "not found"}), 404
    p = one("SELECT * FROM products WHERE id=?", (d.get("product_id") or old["product_id"],))
    qty, price, ship, disc, offsite, fees = sale_values(d, p, settings())
    db().execute("""UPDATE sales SET date=?,order_ref=?,product_id=?,qty=?,unit_price=?,shipping_charged=?,discount=?,
                    postage_cost=?,offsite_ad=?,fees=?,notes=? WHERE id=?""",
                 (d.get("date") or old["date"], d.get("order_ref", ""), p["id"], qty, price, ship, disc,
                  num(d.get("postage_cost")), offsite, fees, d.get("notes", ""), sid))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- expenses
@app.route("/api/expenses", methods=["GET", "POST"])
def api_expenses():
    if request.method == "POST":
        d = request.get_json(force=True)
        db().execute("INSERT INTO expenses(date,category,description,amount) VALUES(?,?,?,?)",
                     (d.get("date") or today(), d.get("category") or "Other", d.get("description", ""), num(d.get("amount"))))
        db().commit()
        return jsonify({"ok": True})
    return jsonify(rows("SELECT * FROM expenses ORDER BY date DESC, id DESC"))


@app.route("/api/expenses/<int:eid>", methods=["DELETE"])
def api_expense(eid):
    db().execute("DELETE FROM expenses WHERE id=?", (eid,))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- dashboard
@app.route("/api/dashboard")
def api_dashboard():
    f = request.args.get("from") or "0000-00-00"
    t = request.args.get("to") or "9999-99-99"
    sales = rows("""SELECT s.*, p.name AS product FROM sales s JOIN products p ON p.id=s.product_id
                    WHERE s.date BETWEEN ? AND ?""", (f, t))
    batches = rows("SELECT b.*, p.name AS product FROM batches b JOIN products p ON p.id=b.product_id WHERE b.date BETWEEN ? AND ?", (f, t))
    exps = rows("SELECT * FROM expenses WHERE date BETWEEN ? AND ?", (f, t))

    def rev(s):
        return s["unit_price"] * s["qty"] + s["shipping_charged"] - s["discount"]

    tot = dict(revenue=0, fees=0, postage=0, cogs=0, items=0, orders=len({s["order_ref"] or f"s{s['id']}" for s in sales}))
    months, prods = {}, {}
    for s in sales:
        r = rev(s)
        c = s["unit_cost"] * s["qty"]
        tot["revenue"] += r
        tot["fees"] += s["fees"]
        tot["postage"] += s["postage_cost"]
        tot["cogs"] += c
        tot["items"] += s["qty"]
        m = months.setdefault(s["date"][:7], dict(month=s["date"][:7], revenue=0, profit=0, expenses=0))
        m["revenue"] += r
        m["profit"] += r - s["fees"] - s["postage_cost"] - c
        pr = prods.setdefault(s["product"], dict(product=s["product"], qty=0, revenue=0, profit=0))
        pr["qty"] += s["qty"]
        pr["revenue"] += r
        pr["profit"] += r - s["fees"] - s["postage_cost"] - c
    waste = sum(b["qty_failed"] * b["unit_cost"] for b in batches)
    spent_printing = sum((b["qty_good"] + b["qty_failed"]) * b["unit_cost"] for b in batches)
    for b in batches:
        m = months.setdefault(b["date"][:7], dict(month=b["date"][:7], revenue=0, profit=0, expenses=0))
        m["profit"] -= b["qty_failed"] * b["unit_cost"]
    exp_total = sum(e["amount"] for e in exps)
    for e in exps:
        m = months.setdefault(e["date"][:7], dict(month=e["date"][:7], revenue=0, profit=0, expenses=0))
        m["expenses"] += e["amount"]
        m["profit"] -= e["amount"]
    gross_profit = tot["revenue"] - tot["fees"] - tot["postage"] - tot["cogs"]
    net = gross_profit - waste - exp_total
    ps = product_rows()
    stock_value = sum(max(p["stock"], 0) * p["unit_cost"] for p in ps)
    low = [dict(name=p["name"], stock=p["stock"], reorder_at=p["reorder_at"]) for p in ps
           if p["active"] and p["reorder_at"] and p["stock"] <= p["reorder_at"]]
    by_cat = {}
    for e in exps:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    return jsonify(dict(
        totals={**{k: round(v, 2) for k, v in tot.items()}, "gross_profit": round(gross_profit, 2),
                "waste": round(waste, 2), "expenses": round(exp_total, 2), "net": round(net, 2),
                "spent_printing": round(spent_printing, 2), "stock_value": round(stock_value, 2),
                "stock_units": sum(max(p["stock"], 0) for p in ps)},
        months=sorted([{k: (round(v, 2) if k != "month" else v) for k, v in m.items()} for m in months.values()], key=lambda x: x["month"]),
        products=sorted([{k: (round(v, 2) if k != "product" else v) for k, v in p.items()} for p in prods.values()], key=lambda x: -x["profit"]),
        low_stock=low,
        expense_categories=[dict(category=k, amount=round(v, 2)) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
    ))


# ---------------------------------------------------------------- etsy csv import
HEADER_MAP = {
    "date": ["sale date", "date paid", "date"],
    "order": ["order id", "order number", "order"],
    "name": ["item name", "title", "listing title"],
    "qty": ["quantity", "qty", "number of items"],
    "price": ["price", "item price", "unit price"],
    "sku": ["sku"],
    "shipping": ["shipping", "order shipping", "shipping price"],
    "discount": ["discount amount", "discount"],
}


def parse_date(v):
    v = (v or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return today()


@app.route("/api/import/etsy", methods=["POST"])
def api_import_etsy():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded."}), 400
    text = f.read().decode("utf-8-sig", errors="replace")
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        return jsonify({"error": "Empty CSV."}), 400
    norm = {h.strip().lower(): h for h in rdr.fieldnames}
    col = {}
    for key, options in HEADER_MAP.items():
        for o in options:
            if o in norm:
                col[key] = norm[o]
                break
    if "name" not in col:
        return jsonify({"error": "Couldn't find an item name column. Use Etsy's 'Order Items' CSV (Shop Manager > Settings > Options > Download Data). Columns found: " + ", ".join(rdr.fieldnames)}), 400
    s = settings()
    added = skipped = created = 0
    seen_orders = set()
    for r in rdr:
        name = (r.get(col["name"]) or "").strip()
        if not name:
            continue
        order = (r.get(col.get("order", ""), "") or "").strip() if "order" in col else ""
        sku = (r.get(col["sku"], "") or "").strip() if "sku" in col else ""
        p = None
        if sku:
            p = one("SELECT * FROM products WHERE sku=? AND sku<>''", (sku,))
        if not p:
            p = one("SELECT * FROM products WHERE lower(name)=lower(?)", (name,))
        if not p:
            price = num(r.get(col.get("price", ""), 0))
            cur = db().execute("INSERT INTO products(name,sku,price) VALUES(?,?,?)", (name, sku, price))
            db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (cur.lastrowid, "price", price))
            p = one("SELECT * FROM products WHERE id=?", (cur.lastrowid,))
            created += 1
        if order and one("SELECT 1 AS x FROM sales WHERE order_ref=? AND product_id=?", (order, p["id"])):
            skipped += 1
            continue
        first = order not in seen_orders
        seen_orders.add(order)
        d = dict(
            qty=num(r.get(col.get("qty", ""), 1), 1) or 1,
            unit_price=num(r.get(col.get("price", ""), p["price"]), p["price"]),
            shipping_charged=num(r.get(col.get("shipping", ""), 0)) if first else 0,
            discount=num(r.get(col.get("discount", ""), 0)) if first else 0,
        )
        qty, price, ship, disc, offsite, fees = sale_values(d, p, s)
        db().execute(
            """INSERT INTO sales(date,order_ref,product_id,qty,unit_price,shipping_charged,discount,postage_cost,
               offsite_ad,fees,unit_cost,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (parse_date(r.get(col.get("date", ""), "")), order, p["id"], qty, price, ship, disc, 0, 0, fees,
             unit_cost_of(p), "etsy-csv"))
        added += 1
    db().commit()
    return jsonify({"added": added, "skipped_duplicates": skipped, "products_created": created})


# ---------------------------------------------------------------- export / backup
@app.route("/api/export/<table>.csv")
def api_export(table):
    if table not in ("sales", "batches", "expenses", "products"):
        return jsonify({"error": "unknown"}), 404
    data = {"sales": lambda: json.loads(api_sales().get_data()),
            "batches": lambda: json.loads(api_batches().get_data()),
            "expenses": lambda: json.loads(api_expenses().get_data()),
            "products": product_rows}[table]()
    out = io.StringIO()
    if data:
        w = csv.DictWriter(out, fieldnames=list(data[0].keys()))
        w.writeheader()
        w.writerows(data)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={table}.csv"})


@app.route("/api/backup")
def api_backup():
    return send_file(DB_PATH, as_attachment=True, download_name="etsy-backup.db")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5055)))
