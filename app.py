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

VERSION = "0.7.0"
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
    "etsy_keystring": "",
    "etsy_secret": "",
    "etsy_redirect_uri": "",
    "etsy_access_token": "",
    "etsy_refresh_token": "",
    "etsy_token_expires": "0",
    "etsy_user_id": "",
    "etsy_shop_id": "",
    "etsy_shop_name": "",
    "etsy_oauth_state": "",
    "etsy_oauth_verifier": "",
    "etsy_last_sync": "",
    "etsy_scopes": "",
    "listing_shipping_profile_id": "",
    "listing_readiness_state_id": "",
    "listing_who_made": "i_did",
    "listing_when_made": "made_to_order",
    "listing_quantity": "5",
    "listing_default_tags": "",
    "listing_materials": "PLA",
    "colour_min_remaining_g": "50",
    "listing_desc_template": "{name}\n\n[Describe what it fits, size, colour options and anything the buyer should check before ordering.]",
}
SECRET_KEYS = ("bambuddy_key", "etsy_secret", "etsy_access_token", "etsy_refresh_token", "etsy_oauth_verifier", "etsy_oauth_state")
INTERNAL_KEYS = ("etsy_access_token", "etsy_refresh_token", "etsy_token_expires", "etsy_user_id", "etsy_shop_id",
                 "etsy_shop_name", "etsy_oauth_state", "etsy_oauth_verifier", "etsy_last_sync", "etsy_scopes")

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
CREATE TABLE IF NOT EXISTS product_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  rank INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS listing_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  title TEXT DEFAULT '',
  description TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  materials TEXT DEFAULT '',
  quantity INTEGER DEFAULT 0,
  taxonomy_id INTEGER,
  taxonomy_name TEXT DEFAULT ''
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


PRODUCT_MIGRATIONS = [
    ("l_title", "TEXT DEFAULT ''"), ("l_desc", "TEXT DEFAULT ''"), ("l_tags", "TEXT DEFAULT ''"),
    ("l_materials", "TEXT DEFAULT ''"), ("l_qty", "INTEGER DEFAULT 0"), ("taxonomy_id", "INTEGER"),
    ("taxonomy_name", "TEXT DEFAULT ''"), ("etsy_listing_id", "INTEGER"), ("etsy_listing_url", "TEXT DEFAULT ''"),
    ("l_var_name", "TEXT DEFAULT ''"), ("l_var_options", "TEXT DEFAULT ''"), ("l_personalisation", "TEXT DEFAULT ''"),
]
TEMPLATE_MIGRATIONS = [
    ("var_name", "TEXT DEFAULT ''"), ("var_source", "TEXT DEFAULT 'none'"), ("var_options", "TEXT DEFAULT ''"),
    ("var_material", "TEXT DEFAULT 'PLA'"), ("personalisation", "TEXT DEFAULT ''"),
]
IMG_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "images")
os.makedirs(IMG_DIR, exist_ok=True)



def _brand_desc(brand_line, range_name, disclaimer_brand):
    return (
        "{name}\n\n"
        f"A practical 3D printed holder made to fit {brand_line}. Keep your workshop, garage or van tidy and your tools and batteries within easy reach.\n\n"
        "✔ DESIGNED FOR\n"
        f"• {range_name}: [list the exact tools / batteries / chargers this fits]\n\n"
        "✔ DETAILS\n"
        "• Material: PLA, printed to order\n"
        "• Size: [dimensions]\n"
        "• Colours: [colours available - choose from the dropdown]\n"
        "• Fixings: [screws included / not included - use screws suitable for your wall]\n\n"
        "✔ GOOD TO KNOW\n"
        "• Made on a 3D printer, so fine layer lines are normal.\n"
        "• Please check the compatibility list above before ordering. Not sure if it fits your model? Message me and I'll happily check.\n"
        "• Colours can look slightly different on different screens.\n\n"
        f"This is an independent product made to fit {disclaimer_brand} tools. It is not made, endorsed or sponsored by {disclaimer_brand}."
    )


SEED_TEMPLATES = [
    ("Einhell Power X-Change holder",
     "{name} | Einhell Power X-Change 18V Holder | 3D Printed Wall Mount | Workshop & Garage Storage",
     _brand_desc("Einhell Power X-Change (PXC) 18V tools and batteries", "Einhell Power X-Change 18V range", "Einhell"),
     "einhell holder, power x change, einhell pxc, 18v battery holder, tool wall mount, pxc battery mount, workshop storage, garage organiser, tool storage, 3d printed, van storage, gift for diyer, diy gift"),
    ("Titan TXP (Screwfix) holder",
     "{name} | Titan TXP 18V Holder | 3D Printed Wall Mount | Workshop & Garage Storage",
     _brand_desc("Titan TXP 18V tools and batteries (sold at Screwfix)", "Titan TXP 18V range", "Titan or Screwfix"),
     "titan txp, titan 18v, titan holder, 18v battery holder, tool wall mount, screwfix tools, workshop storage, garage organiser, tool storage, 3d printed, van storage, gift for diyer, diy gift"),
    ("Erbauer EXT (Screwfix) holder",
     "{name} | Erbauer EXT 18V Holder | 3D Printed Wall Mount | Workshop & Garage Storage",
     _brand_desc("Erbauer EXT 18V tools and batteries (sold at Screwfix)", "Erbauer EXT 18V range", "Erbauer or Screwfix"),
     "erbauer ext, erbauer 18v, erbauer holder, 18v battery holder, tool wall mount, screwfix tools, workshop storage, garage organiser, tool storage, 3d printed, van storage, gift for diyer, diy gift"),
    ("Generic tool holder / wall mount",
     "{name} | 3D Printed Tool Holder | Wall Mount Storage for Workshop, Garage & Shed",
     "{name}\n\nA practical 3D printed holder to keep your tools organised and easy to reach.\n\n"
     "✔ DETAILS\n• Material: PLA, printed to order\n• Size: [dimensions]\n• Holds: [what it holds / fits]\n• Colours: [colours available - choose from the dropdown]\n"
     "• Fixings: [screws included / not included]\n\n✔ GOOD TO KNOW\n• Made on a 3D printer, so fine layer lines are normal.\n"
     "• Please check the size and fit details above before ordering. Questions? Message me and I'll help.\n• Colours can look slightly different on different screens.",
     "tool holder, tool wall mount, tool storage, workshop storage, garage organiser, shed storage, 3d printed, wall mounted, tool organiser, gift for diyer, diy gift, van storage, workshop gift"),
    ("Replacement part / accessory",
     "{name} | 3D Printed Replacement Part for [brand and model] | Spare Part Accessory",
     "{name}\n\nA 3D printed replacement part / accessory made to fit [brand and model].\n\n"
     "✔ FITS\n• [brand, model and part number it replaces]\n\n✔ DETAILS\n• Material: PLA, printed to order [use PETG if more heat resistance is needed]\n"
     "• Dimensions: [key measurements]\n• Colour: [colour]\n\n✔ BEFORE YOU ORDER\n"
     "• Please measure your original part and compare it with the dimensions above.\n• 3D printed parts are not identical to injection-moulded originals and may not suit high-load or high-heat uses.\n"
     "• Unsure? Message me with your model number and I'll confirm before you buy.\n\nThis is an independent replacement part. It is not made or endorsed by the original manufacturer.",
     "replacement part, spare part, 3d printed part, vacuum accessory, hoover spare, repair part, diy repair, custom fit part, made to order, appliance spare, tool accessory, fix it, workshop part"),
]


def seed_templates(con):
    if con.execute("SELECT COUNT(*) FROM listing_templates").fetchone()[0] == 0:
        for name, title, desc, tags in SEED_TEMPLATES:
            con.execute("""INSERT INTO listing_templates(name,title,description,tags,materials,quantity,var_name,var_source,var_material,personalisation)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (name, title, desc, tags, "PLA", 5, "Colour", "spools", "PLA", ""))


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(products)")}
    for col, decl in PRODUCT_MIGRATIONS:
        if col not in have:
            con.execute(f"ALTER TABLE products ADD COLUMN {col} {decl}")
    have_t = {r[1] for r in con.execute("PRAGMA table_info(listing_templates)")}
    for col, decl in TEMPLATE_MIGRATIONS:
        if col not in have_t:
            con.execute(f"ALTER TABLE listing_templates ADD COLUMN {col} {decl}")
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    seed_templates(con)
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
            if k not in DEFAULT_SETTINGS or k in INTERNAL_KEYS:
                continue
            if k in SECRET_KEYS and not v:
                continue  # blank = keep existing secret
            set_setting(k, v)
        db().commit()
    s = settings()
    for k in SECRET_KEYS:
        s[k + "_set"] = bool(s.get(k))
        s[k] = ""  # never send secrets back to the browser
    s["etsy_connected"] = bool(s.get("etsy_refresh_token_set") and s.get("etsy_shop_id"))
    s["etsy_can_list"] = "listings_w" in (s.get("etsy_scopes") or "")
    return jsonify(s)


@app.route("/api/version")
def api_version():
    return jsonify({"version": VERSION})


def set_setting(k, v):
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))


# ---------------------------------------------------------------- products
def product_rows():
    ps = rows("""
      SELECT p.*,
        COALESCE((SELECT SUM(qty_good) FROM batches b WHERE b.product_id=p.id),0) AS made,
        COALESCE((SELECT SUM(qty_failed) FROM batches b WHERE b.product_id=p.id),0) AS failed,
        COALESCE((SELECT SUM(qty) FROM sales s WHERE s.product_id=p.id),0) AS sold,
        (SELECT COUNT(*) FROM product_images i WHERE i.product_id=p.id) AS n_images
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
        raise BBError(f"Bambuddy returned HTTP {e.code} for {url.split('?')[0]} - check the URL is Bambuddy's own address/port (e.g. http://192.168.1.6:8000) and the API key is valid.")
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
        "objects": int(num(a.get("object_count"))) or None,
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
            data = bb_get("/archives/", {"limit": 50})
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify([norm_archive(a, s) for a in as_list(data)])


@app.route("/api/bambuddy/test")
def api_bb_test():
    try:
        a = as_list(bb_get("/archives/", {"limit": 1}))
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


# ---------------------------------------------------------------- etsy api
import base64
import hashlib
import secrets
import time

ETSY_API = "https://api.etsy.com/v3/application"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_SCOPES = "transactions_r shops_r listings_r listings_w"


class EtsyError(Exception):
    pass


def etsy_headers(s, token=None):
    h = {"x-api-key": f"{s['etsy_keystring']}:{s['etsy_secret']}", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def http_json(url, headers=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise EtsyError(f"Etsy returned HTTP {e.code}: {body}")
    except Exception as e:
        raise EtsyError(f"Could not reach Etsy: {e}")


def etsy_token_request(form):
    s = settings()
    tok = http_json(ETSY_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, form=form)
    set_setting("etsy_access_token", tok["access_token"])
    set_setting("etsy_refresh_token", tok["refresh_token"])
    set_setting("etsy_token_expires", int(time.time()) + int(tok.get("expires_in", 3600)))
    set_setting("etsy_user_id", tok["access_token"].split(".")[0])
    db().commit()


def etsy_get(path, params=None):
    s = settings()
    if not (s.get("etsy_keystring") and s.get("etsy_secret")):
        raise EtsyError("Enter your Etsy keystring and shared secret in Settings first.")
    if not s.get("etsy_refresh_token"):
        raise EtsyError("Etsy isn't connected yet.")
    if int(num(s.get("etsy_token_expires"))) < time.time() + 120:
        etsy_token_request({"grant_type": "refresh_token", "client_id": s["etsy_keystring"], "refresh_token": s["etsy_refresh_token"]})
        s = settings()
    url = ETSY_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return http_json(url, headers=etsy_headers(s, s["etsy_access_token"]))


@app.route("/api/etsy/auth-url", methods=["POST"])
def api_etsy_auth_url():
    s = settings()
    if not s.get("etsy_keystring") or not s.get("etsy_secret") or not s.get("etsy_redirect_uri"):
        return jsonify({"error": "Save your keystring, shared secret and redirect URL first."}), 400
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    set_setting("etsy_oauth_verifier", verifier)
    set_setting("etsy_oauth_state", state)
    db().commit()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": s["etsy_keystring"], "redirect_uri": s["etsy_redirect_uri"],
                                "scope": ETSY_SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"},
                               quote_via=urllib.parse.quote)
    return jsonify({"url": "https://www.etsy.com/oauth/connect?" + q})


@app.route("/api/etsy/connect", methods=["POST"])
def api_etsy_connect():
    s = settings()
    raw = (request.get_json(force=True).get("url") or "").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query) if "?" in raw else {}
    code = (qs.get("code") or [raw if "=" not in raw else ""])[0]
    state = (qs.get("state") or [""])[0]
    if not code:
        return jsonify({"error": "Couldn't find a code in what you pasted. Paste the full address from the browser bar."}), 400
    if state and state != s.get("etsy_oauth_state"):
        return jsonify({"error": "State mismatch - click 'Open Etsy login' again and use the newest redirect."}), 400
    try:
        etsy_token_request({"grant_type": "authorization_code", "client_id": s["etsy_keystring"], "redirect_uri": s["etsy_redirect_uri"],
                            "code": code, "code_verifier": s.get("etsy_oauth_verifier", "")})
        uid = settings()["etsy_user_id"]
        shop = etsy_get(f"/users/{uid}/shops")
        if "results" in shop and shop["results"]:
            shop = shop["results"][0]
        set_setting("etsy_scopes", ETSY_SCOPES)
        set_setting("etsy_shop_id", shop["shop_id"])
        set_setting("etsy_shop_name", shop.get("shop_name", ""))
        db().commit()
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "shop": shop.get("shop_name")})


@app.route("/api/etsy/disconnect", methods=["POST"])
def api_etsy_disconnect():
    for k in ("etsy_access_token", "etsy_refresh_token", "etsy_user_id", "etsy_shop_id", "etsy_shop_name"):
        set_setting(k, "")
    set_setting("etsy_token_expires", "0")
    db().commit()
    return jsonify({"ok": True})


def money_of(m):
    if not m:
        return 0.0
    return num(m.get("amount")) / (num(m.get("divisor"), 100) or 100)


@app.route("/api/etsy/sync", methods=["POST"])
def api_etsy_sync():
    s = settings()
    shop = s.get("etsy_shop_id")
    if not shop:
        return jsonify({"error": "Etsy isn't connected yet."}), 400
    body = request.get_json(silent=True) or {}
    params_base = {"limit": 100, "sort_on": "created", "sort_order": "asc", "was_canceled": "false"}
    if body.get("full"):
        pass
    elif s.get("etsy_last_sync"):
        params_base["min_created"] = max(int(num(s["etsy_last_sync"])) - 86400 * 2, 0)
    added = skipped = created = refunded = 0
    offset = 0
    newest = int(num(s.get("etsy_last_sync")))
    try:
        while True:
            page = etsy_get(f"/shops/{shop}/receipts", {**params_base, "offset": offset})
            results = page.get("results", [])
            for r in results:
                ts = int(r.get("created_timestamp") or r.get("create_timestamp") or 0)
                newest = max(newest, ts)
                if not r.get("is_paid"):
                    continue
                if r.get("refunds"):
                    refunded += 1
                order = str(r["receipt_id"])
                date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else today()
                first = True
                for t in r.get("transactions", []):
                    name = (t.get("title") or "Etsy item").strip()
                    sku = (t.get("sku") or "").strip()
                    p = one("SELECT * FROM products WHERE sku=? AND sku<>''", (sku,)) if sku else None
                    if not p:
                        p = one("SELECT * FROM products WHERE lower(name)=lower(?)", (name,))
                    if not p:
                        price = money_of(t.get("price"))
                        cur = db().execute("INSERT INTO products(name,sku,price) VALUES(?,?,?)", (name, sku, price))
                        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (cur.lastrowid, "price", price))
                        p = one("SELECT * FROM products WHERE id=?", (cur.lastrowid,))
                        created += 1
                    if one("SELECT 1 AS x FROM sales WHERE order_ref=? AND product_id=?", (order, p["id"])):
                        skipped += 1
                        first = False
                        continue
                    d = dict(qty=t.get("quantity") or 1, unit_price=money_of(t.get("price")),
                             shipping_charged=money_of(r.get("total_shipping_cost")) if first else 0,
                             discount=money_of(r.get("discount_amt")) if first else 0)
                    first = False
                    qty, price, ship, disc, offsite, fees = sale_values(d, p, s)
                    db().execute(
                        """INSERT INTO sales(date,order_ref,product_id,qty,unit_price,shipping_charged,discount,postage_cost,
                           offsite_ad,fees,unit_cost,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (date, order, p["id"], qty, price, ship, disc, 0, 0, fees, unit_cost_of(p), "etsy-api"))
                    added += 1
            db().commit()
            if len(results) < 100:
                break
            offset += 100
    except EtsyError as e:
        db().commit()
        return jsonify({"error": str(e)}), 502
    except KeyError as e:
        db().commit()
        return jsonify({"error": f"Unexpected Etsy response (missing {e}). Use 'Show ledger/receipt sample' and send me the output."}), 502
    if newest:
        set_setting("etsy_last_sync", newest)
        db().commit()
    return jsonify({"added": added, "skipped_duplicates": skipped, "products_created": created, "receipts_with_refunds": refunded})


@app.route("/api/etsy/sample")
def api_etsy_sample():
    """Debug helper: latest ledger entries + one receipt, so real field values can be inspected."""
    shop = settings().get("etsy_shop_id")
    try:
        ledger = etsy_get(f"/shops/{shop}/payment-account/ledger-entries", {"limit": 15, "min_created": int(time.time()) - 86400 * 60, "max_created": int(time.time())})
    except EtsyError as e:
        ledger = {"error": str(e)}
    try:
        rec = etsy_get(f"/shops/{shop}/receipts", {"limit": 1, "sort_order": "desc"})
        for r in rec.get("results", []):
            for k in ("buyer_email", "name", "first_line", "second_line", "city", "state", "zip", "formatted_address", "seller_email", "payment_email", "buyer_user_id", "message_from_buyer"):
                r.pop(k, None)  # strip personal data
    except EtsyError as e:
        rec = {"error": str(e)}
    return jsonify({"ledger": ledger, "receipt": rec})


# ---------------------------------------------------------------- listings (Etsy drafts)
import mimetypes
import re
import uuid

TAX_CACHE = {"nodes": None}
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".gif"}


def etsy_auth_headers():
    """Valid headers with a fresh access token (refreshes if needed)."""
    s = settings()
    if not (s.get("etsy_keystring") and s.get("etsy_secret")):
        raise EtsyError("Enter your Etsy keystring and shared secret in Settings first.")
    if not s.get("etsy_refresh_token"):
        raise EtsyError("Etsy isn't connected yet.")
    if int(num(s.get("etsy_token_expires"))) < time.time() + 120:
        etsy_token_request({"grant_type": "refresh_token", "client_id": s["etsy_keystring"], "refresh_token": s["etsy_refresh_token"]})
        s = settings()
    return s, etsy_headers(s, s["etsy_access_token"])


def etsy_send(method, path, form=None, files=None, fields=None, json_body=None):
    s, h = etsy_auth_headers()
    url = ETSY_API + path
    if files:
        boundary = "----etsytracker" + uuid.uuid4().hex
        parts = []
        for k, v in (fields or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content, ctype) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    else:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        if data is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        hint = " (Etsy didn't allow this. If it mentions scope, go to Settings and re-authorise Etsy.)" if e.code in (401, 403) else ""
        raise EtsyError(f"Etsy returned HTTP {e.code}: {body}{hint}")
    except Exception as e:
        raise EtsyError(f"Could not reach Etsy: {e}")


@app.route("/api/etsy/shipping-profiles")
def api_etsy_shipping():
    shop = settings().get("etsy_shop_id")
    try:
        d = etsy_get(f"/shops/{shop}/shipping-profiles")
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify([{"id": r["shipping_profile_id"], "title": r.get("title", "")} for r in d.get("results", [])])


@app.route("/api/etsy/processing-profiles")
def api_etsy_processing():
    shop = settings().get("etsy_shop_id")
    try:
        d = etsy_get(f"/shops/{shop}/readiness-state-definitions", {"limit": 100})
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    out = []
    for r in d.get("results", []):
        label = r.get("processing_days_display_label") or f"{r.get('min_processing_days')}-{r.get('max_processing_days')} days"
        out.append({"id": r["readiness_state_id"], "title": f"{(r.get('readiness_state') or '').replace('_', ' ')} · {label}"})
    return jsonify(out)


def load_taxonomy():
    if TAX_CACHE["nodes"] is not None:
        return TAX_CACHE["nodes"]
    path = os.path.join(os.path.dirname(DB_PATH) or ".", "taxonomy.json")
    if os.path.exists(path):
        TAX_CACHE["nodes"] = json.load(open(path))
        return TAX_CACHE["nodes"]
    s = settings()
    if not s.get("etsy_keystring"):
        raise EtsyError("Enter your Etsy keystring first.")
    tree = http_json(ETSY_API + "/seller-taxonomy/nodes", headers=etsy_headers(s))
    flat = []

    def walk(node, trail):
        t = trail + [node["name"]]
        if node.get("children"):
            for c in node["children"]:
                walk(c, t)
        else:
            flat.append({"id": node["id"], "path": " › ".join(t)})

    for n in tree.get("results", []):
        walk(n, [])
    json.dump(flat, open(path, "w"))
    TAX_CACHE["nodes"] = flat
    return flat


@app.route("/api/etsy/taxonomy")
def api_etsy_taxonomy():
    q = request.args.get("q", "").lower().split()
    if not q:
        return jsonify([])
    try:
        nodes = load_taxonomy()
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    hits = [n for n in nodes if all(w in n["path"].lower() for w in q)]
    hits.sort(key=lambda n: len(n["path"]))
    return jsonify(hits[:25])


@app.route("/api/products/<int:pid>/listing", methods=["PUT"])
def api_listing_save(pid):
    d = request.get_json(force=True)
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    db().execute("""UPDATE products SET l_title=?,l_desc=?,l_tags=?,l_materials=?,l_qty=?,taxonomy_id=?,taxonomy_name=?,
                    l_var_name=?,l_var_options=?,l_personalisation=? WHERE id=?""",
                 (d.get("l_title", "")[:140], d.get("l_desc", ""), d.get("l_tags", ""), d.get("l_materials", ""),
                  int(num(d.get("l_qty"))), d.get("taxonomy_id") or None, d.get("taxonomy_name", ""),
                  d.get("l_var_name", ""), d.get("l_var_options", ""), d.get("l_personalisation", ""), pid))
    db().commit()
    return jsonify({"ok": True})


# --- images
@app.route("/api/products/<int:pid>/images", methods=["GET", "POST"])
def api_images(pid):
    if request.method == "POST":
        n = one("SELECT COALESCE(MAX(rank),0) AS m FROM product_images WHERE product_id=?", (pid,))["m"]
        for f in request.files.getlist("files"):
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in ALLOWED_IMG:
                return jsonify({"error": f"{f.filename}: only JPG, PNG or GIF images are accepted."}), 400
            data = f.read()
            if len(data) > 10 * 1024 * 1024:
                return jsonify({"error": f"{f.filename} is over Etsy's 10MB limit."}), 400
            name = uuid.uuid4().hex + ext
            open(os.path.join(IMG_DIR, name), "wb").write(data)
            n += 1
            db().execute("INSERT INTO product_images(product_id,filename,rank) VALUES(?,?,?)", (pid, name, n))
        db().commit()
    return jsonify(rows("SELECT id,rank FROM product_images WHERE product_id=? ORDER BY rank,id", (pid,)))


@app.route("/api/images/<int:iid>/file")
def api_image_file(iid):
    r = one("SELECT filename FROM product_images WHERE id=?", (iid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(IMG_DIR, r["filename"])


@app.route("/api/images/<int:iid>", methods=["DELETE"])
def api_image_delete(iid):
    r = one("SELECT filename FROM product_images WHERE id=?", (iid,))
    if r:
        try:
            os.remove(os.path.join(IMG_DIR, r["filename"]))
        except OSError:
            pass
        db().execute("DELETE FROM product_images WHERE id=?", (iid,))
        db().commit()
    return jsonify({"ok": True})


@app.route("/api/images/<int:iid>/first", methods=["POST"])
def api_image_first(iid):
    r = one("SELECT product_id FROM product_images WHERE id=?", (iid,))
    if r:
        m = one("SELECT COALESCE(MIN(rank),1) AS m FROM product_images WHERE product_id=?", (r["product_id"],))["m"]
        db().execute("UPDATE product_images SET rank=? WHERE id=?", (m - 1, iid))
        db().commit()
    return jsonify({"ok": True})


def clean_list(text, maxlen, maxn):
    out, seen = [], set()
    for t in re.split(r"[,\n]", text or ""):
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:maxlen])
    return out[:maxn]


@app.route("/api/products/<int:pid>/etsy-draft", methods=["POST"])
def api_etsy_draft(pid):
    s = settings()
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    if p["etsy_listing_id"]:
        return jsonify({"error": "A draft already exists for this product. Open it on Etsy to edit it."}), 400
    if "listings_w" not in (s.get("etsy_scopes") or ""):
        return jsonify({"error": "Etsy needs re-authorising with permission to create listings (Settings → Etsy connection)."}), 400
    problems = []
    if not p["taxonomy_id"]:
        problems.append("choose a category")
    if num(p["price"]) <= 0:
        problems.append("set a price on the product")
    if not s.get("listing_shipping_profile_id"):
        problems.append("choose a default shipping profile in Settings")
    if problems:
        return jsonify({"error": "Before creating the draft: " + ", ".join(problems) + "."}), 400
    title = (p["l_title"] or p["name"]).strip()[:140]
    desc = p["l_desc"] or (s.get("listing_desc_template") or "{name}").replace("{name}", p["name"])
    tags = clean_list((p["l_tags"] or "") + "," + (s.get("listing_default_tags") or ""), 20, 13)
    mats = clean_list(p["l_materials"] or s.get("listing_materials"), 45, 13)
    qty = int(p["l_qty"] or num(s.get("listing_quantity"), 1)) or 1
    form = {"quantity": qty, "title": title, "description": desc, "price": f"{p['price']:.2f}",
            "who_made": s.get("listing_who_made") or "i_did", "when_made": s.get("listing_when_made") or "made_to_order",
            "taxonomy_id": p["taxonomy_id"], "shipping_profile_id": s["listing_shipping_profile_id"],
            "type": "physical", "is_supply": "false", "should_auto_renew": "false"}
    if s.get("listing_readiness_state_id"):
        form["readiness_state_id"] = s["listing_readiness_state_id"]
    if tags:
        form["tags"] = ",".join(tags)
    if mats:
        form["materials"] = ",".join(mats)
    per_item_g = num(p["plate_grams"]) / max(p["items_per_plate"] or 1, 1)
    if per_item_g > 0:
        form["item_weight"] = round(per_item_g, 1)
        form["item_weight_unit"] = "g"
    try:
        res = etsy_send("POST", f"/shops/{s['etsy_shop_id']}/listings", form=form)
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    lid = res.get("listing_id")
    url = f"https://www.etsy.com/your/shops/me/tools/listings/{lid}"
    db().execute("UPDATE products SET etsy_listing_id=?, etsy_listing_url=? WHERE id=?", (lid, url, pid))
    db().commit()
    warnings = []
    for im in rows("SELECT * FROM product_images WHERE product_id=? ORDER BY rank,id", (pid,)):
        path = os.path.join(IMG_DIR, im["filename"])
        try:
            ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
            etsy_send("POST", f"/shops/{s['etsy_shop_id']}/listings/{lid}/images",
                      fields={"rank": str(im["rank"] if im["rank"] > 0 else 1)},
                      files={"image": (im["filename"], open(path, "rb").read(), ctype)})
        except (EtsyError, OSError) as e:
            warnings.append(str(e))
    options = [o.strip() for o in (p["l_var_options"] or "").split("|") if o.strip()]
    if options and (p["l_var_name"] or "").strip():
        try:
            push_variations(s, lid, p, options, qty)
        except EtsyError as e:
            warnings.append("Variations weren't added: " + str(e))
    return jsonify({"ok": True, "listing_id": lid, "url": url, "warnings": warnings,
                    "personalisation": p["l_personalisation"] or "", "variations": len(options)})


def push_variations(s, lid, p, options, qty):
    vname = re.sub(r"[()]", "", p["l_var_name"].strip())[:45]
    seen, products = set(), []
    for o in options:
        o = re.sub(r"[()]", "", o).strip()[:20]
        if not o or o.lower() in seen:
            continue
        seen.add(o.lower())
        off = {"price": round(float(p["price"]), 2), "quantity": qty, "is_enabled": True}
        if s.get("listing_readiness_state_id"):
            off["readiness_state_id"] = int(s["listing_readiness_state_id"])
        prod = {"property_values": [{"property_id": 513, "property_name": vname, "value_ids": [], "values": [o]}], "offerings": [off]}
        if p["sku"]:
            prod["sku"] = (p["sku"] + "-" + re.sub(r"[^A-Za-z0-9]+", "", o).upper())[:32]
        products.append(prod)
    if not products:
        return
    etsy_send("PUT", f"/listings/{lid}/inventory", json_body={"products": products, "price_on_property": [], "quantity_on_property": [], "sku_on_property": []})


@app.route("/api/products/<int:pid>/unlink-listing", methods=["POST"])
def api_unlink_listing(pid):
    db().execute("UPDATE products SET etsy_listing_id=NULL, etsy_listing_url='' WHERE id=?", (pid,))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- listing templates
@app.route("/api/templates", methods=["GET", "POST"])
def api_templates():
    if request.method == "POST":
        d = request.get_json(force=True)
        cur = db().execute(
            """INSERT INTO listing_templates(name,title,description,tags,materials,quantity,taxonomy_id,taxonomy_name,
               var_name,var_source,var_options,var_material,personalisation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tpl_values(d))
        db().commit()
        return jsonify({"id": cur.lastrowid})
    return jsonify(rows("SELECT * FROM listing_templates ORDER BY id"))


def tpl_values(d):
    return ((d.get("name", "").strip() or "New template"), d.get("title", ""), d.get("description", ""), d.get("tags", ""),
            d.get("materials", ""), int(num(d.get("quantity"))), d.get("taxonomy_id") or None, d.get("taxonomy_name", ""),
            d.get("var_name", ""), d.get("var_source", "none"), d.get("var_options", ""), d.get("var_material", "PLA"),
            d.get("personalisation", ""))


@app.route("/api/templates/<int:tid>", methods=["PUT", "DELETE"])
def api_template(tid):
    if request.method == "DELETE":
        db().execute("DELETE FROM listing_templates WHERE id=?", (tid,))
        db().commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    db().execute("""UPDATE listing_templates SET name=?,title=?,description=?,tags=?,materials=?,quantity=?,taxonomy_id=?,taxonomy_name=?,
                    var_name=?,var_source=?,var_options=?,var_material=?,personalisation=? WHERE id=?""", tpl_values(d) + (tid,))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/bambuddy/colours")
def api_bb_colours():
    """Filament colours you currently have (non-archived spools with enough left), grouped by colour name."""
    material = request.args.get("material", "").strip().lower()
    s = settings()
    try:
        spools = as_list(bb_get("/inventory/spools"))
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    min_g = num(s.get("colour_min_remaining_g"), 50)
    out = {}
    for sp in spools:
        if sp.get("archived_at"):
            continue
        if material and (sp.get("material") or "").strip().lower() != material:
            continue
        name = (sp.get("color_name") or "").strip()
        if not name:
            continue
        remaining = max(num(sp.get("label_weight"), 1000) - num(sp.get("weight_used")), 0)
        c = out.setdefault(name.lower(), {"name": name, "grams": 0.0, "spools": 0, "rgba": sp.get("rgba") or ""})
        c["grams"] += remaining
        c["spools"] += 1
    res = [dict(c, grams=round(c["grams"]), in_stock=c["grams"] >= min_g) for c in out.values()]
    return jsonify(sorted(res, key=lambda c: (-c["in_stock"], c["name"].lower())))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5055)))
