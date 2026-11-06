import csv
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory

VERSION = "0.24.0"
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
    "listing_return_policy_id": "",
    "listing_shop_section_id": "",
    "listing_auto_renew": "0",
    "listing_readiness_state_id": "",
    "listing_who_made": "i_did",
    "listing_when_made": "made_to_order",
    "listing_quantity": "5",
    "listing_default_tags": "",
    "listing_materials": "PLA",
    "colour_min_remaining_g": "50",
    "goal_amount": "2000",
    "electricity_price_per_kwh": "0.27",
    "labour_rate_per_hour": "0",
    "ship_rates": "",
    "backup_enabled": "0",
    "backup_repo": "",
    "backup_token": "",
    "backup_branch": "main",
    "backup_every_hours": "24",
    "backup_last": "",
    "backup_status": "",
    "default_postage_cost": "0",
    "default_shipping_charged": "0",
    "target_margin_pct": "40",
    "listing_desc_template": "{name}\n\n[Describe what it fits, size, colour options and anything the buyer should check before ordering.]",
}
SECRET_KEYS = ("backup_token", "bambuddy_key", "etsy_secret", "etsy_access_token", "etsy_refresh_token", "etsy_oauth_verifier", "etsy_oauth_state")
INTERNAL_KEYS = ("etsy_access_token", "etsy_refresh_token", "etsy_token_expires", "etsy_user_id", "etsy_shop_id",
                 "etsy_shop_name", "backup_last", "backup_status", "etsy_oauth_state", "etsy_oauth_verifier", "etsy_last_sync", "etsy_scopes")

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
CREATE TABLE IF NOT EXISTS supplies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  link TEXT DEFAULT '',
  unit_cost REAL DEFAULT 0,
  stock REAL DEFAULT 0,
  counted_sale_id INTEGER DEFAULT 0,
  counted_at TEXT DEFAULT '',
  lead_days INTEGER DEFAULT 0,
  per_order REAL DEFAULT 0,
  manual_week REAL DEFAULT 0,
  notes TEXT DEFAULT '',
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS product_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  orig_name TEXT NOT NULL,
  size INTEGER DEFAULT 0,
  added_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS measure_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT DEFAULT '',
  product_id INTEGER,
  notes TEXT DEFAULT '',
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS measure_values (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  value TEXT DEFAULT '',
  unit TEXT DEFAULT 'mm',
  rank INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS product_supplies (
  product_id INTEGER NOT NULL,
  supply_id INTEGER NOT NULL,
  qty REAL DEFAULT 1,
  PRIMARY KEY (product_id, supply_id)
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
    ("plate_energy", "REAL DEFAULT 0"), ("labour_minutes", "INTEGER DEFAULT 0"),
    ("model_url", "TEXT DEFAULT ''"),
    ("l_return_policy", "TEXT DEFAULT ''"), ("l_shop_section", "TEXT DEFAULT ''"),
    ("l_ship_profile", "TEXT DEFAULT ''"), ("l_readiness", "TEXT DEFAULT ''"),
    ("item_l", "REAL DEFAULT 0"), ("item_w", "REAL DEFAULT 0"), ("item_h", "REAL DEFAULT 0"),
    ("pack_g", "REAL DEFAULT 0"), ("pack_l", "REAL DEFAULT 0"), ("pack_w", "REAL DEFAULT 0"), ("pack_h", "REAL DEFAULT 0"),
    ("postage_cost", "REAL DEFAULT 0"),
    ("etsy_state", "TEXT DEFAULT ''"), ("etsy_views", "INTEGER DEFAULT 0"), ("etsy_favs", "INTEGER DEFAULT 0"),
    ("etsy_qty", "INTEGER"), ("etsy_synced", "TEXT DEFAULT ''"),
    ("l_var_name", "TEXT DEFAULT ''"), ("l_var_options", "TEXT DEFAULT ''"), ("l_personalisation", "TEXT DEFAULT ''"),
]
TEMPLATE_MIGRATIONS = [
    ("var_name", "TEXT DEFAULT ''"), ("var_source", "TEXT DEFAULT 'none'"), ("var_options", "TEXT DEFAULT ''"),
    ("var_material", "TEXT DEFAULT 'PLA'"), ("personalisation", "TEXT DEFAULT ''"),
]
SALE_MIGRATIONS = [("status", "TEXT DEFAULT 'shipped'"), ("variation", "TEXT DEFAULT ''"),
                   ("tracking", "TEXT DEFAULT ''"), ("shipped_at", "TEXT DEFAULT ''")]
IMG_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "images")
FILE_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "files")
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
    have_s = {r[1] for r in con.execute("PRAGMA table_info(sales)")}
    for col, decl in SALE_MIGRATIONS:
        if col not in have_s:
            con.execute(f"ALTER TABLE sales ADD COLUMN {col} {decl}")
    have_im = {r[1] for r in con.execute("PRAGMA table_info(product_images)")}
    if "etsy_image_id" not in have_im:
        con.execute("ALTER TABLE product_images ADD COLUMN etsy_image_id INTEGER")
    have_sup = {r[1] for r in con.execute("PRAGMA table_info(supplies)")}
    for col in ("box_l", "box_w", "box_h", "box_g"):
        if col not in have_sup:
            con.execute(f"ALTER TABLE supplies ADD COLUMN {col} REAL DEFAULT 0")
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


def labour_rate():
    if "labour_rate" not in g:
        g.labour_rate = num(settings().get("labour_rate_per_hour"))
    return g.labour_rate


def unit_cost_of(p):
    per_plate = num(p.get("plate_cost")) + num(p.get("plate_energy"))
    ipp = max(int(num(p.get("items_per_plate"), 1)), 1)
    return round(per_plate / ipp + num(p.get("extra_cost")) + num(p.get("labour_minutes")) / 60.0 * labour_rate(), 4)


def stock_of(pid):
    r = one("""SELECT COALESCE((SELECT SUM(qty_good) FROM batches WHERE product_id=?),0) AS made,
                      COALESCE((SELECT SUM(qty) FROM sales WHERE product_id=?),0) AS sold""", (pid, pid))
    return r["made"] - r["sold"]


def initial_status(pid):
    """After a sale is saved: if we are not short of stock the order can ship, otherwise it needs printing."""
    return "to_ship" if stock_of(pid) >= 0 else "to_print"


def release_backorders(pid):
    """After printing: move the oldest waiting lines to 'to_ship' while stock covers them."""
    waiting = rows("SELECT id,qty FROM sales WHERE product_id=? AND status IN ('to_print','printing') ORDER BY date,id", (pid,))
    if not waiting:
        return
    backlog = sum(w["qty"] for w in waiting)
    available = stock_of(pid) + backlog  # units made that are not already allocated to shipped/ready orders
    for w in waiting:
        if available >= w["qty"]:
            db().execute("UPDATE sales SET status='to_ship' WHERE id=?", (w["id"],))
            available -= w["qty"]
    db().commit()


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
    s["etsy_can_ship"] = "transactions_w" in (s.get("etsy_scopes") or "")
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
               items_per_plate,extra_cost,print_minutes,reorder_at,active,notes,plate_energy,labour_minutes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("name", "").strip() or "Untitled", d.get("sku", ""), num(d.get("price")),
             d.get("archive_id") or None, d.get("archive_name", ""), num(d.get("plate_grams")),
             num(d.get("plate_cost")), max(int(num(d.get("items_per_plate"), 1)), 1),
             num(d.get("extra_cost")), int(num(d.get("print_minutes"))), int(num(d.get("reorder_at"))),
             1 if d.get("active", 1) else 0, d.get("notes", ""), num(d.get("plate_energy")), int(num(d.get("labour_minutes")))))
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
        db().execute("DELETE FROM product_supplies WHERE product_id=?", (pid,))
        for fr in rows("SELECT filename FROM product_files WHERE product_id=?", (pid,)):
            try:
                os.remove(os.path.join(FILE_DIR, fr["filename"]))
            except OSError:
                pass
        db().execute("DELETE FROM product_files WHERE product_id=?", (pid,))
        db().execute("DELETE FROM products WHERE id=?", (pid,))
        db().commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    new = dict(old)
    for k in ("name", "sku", "archive_name", "notes"):
        if k in d:
            new[k] = d[k]
    for k in ("price", "plate_grams", "plate_cost", "extra_cost", "plate_energy"):
        if k in d:
            new[k] = num(d[k])
    for k in ("items_per_plate", "print_minutes", "reorder_at", "labour_minutes"):
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
           items_per_plate=?,extra_cost=?,print_minutes=?,reorder_at=?,active=?,notes=?,plate_energy=?,labour_minutes=? WHERE id=?""",
        (new["name"], new["sku"], new["price"], new["archive_id"], new["archive_name"], new["plate_grams"],
         new["plate_cost"], new["items_per_plate"], new["extra_cost"], new["print_minutes"], new["reorder_at"],
         new["active"], new["notes"], new["plate_energy"], new["labour_minutes"], new["id"]))
    if abs(num(new["price"]) - num(old["price"])) > 1e-9:
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (new["id"], "price", new["price"]))
    if abs(unit_cost_of(new) - unit_cost_of(old)) > 1e-9:
        db().execute("INSERT INTO history(product_id,kind,value) VALUES(?,?,?)", (new["id"], "cost", unit_cost_of(new)))
    db().commit()


@app.route("/api/history")
def api_history_all():
    return jsonify(rows("SELECT product_id,kind,value,changed_at FROM history ORDER BY changed_at,id"))


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
        "energy": round(num(a.get("energy_cost")) or num(a.get("energy_kwh")) * num(s.get("electricity_price_per_kwh")), 4),
        "energy_kwh": num(a.get("energy_kwh")),
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
    new.update(plate_grams=n["grams"], plate_cost=n["cost"], archive_name=n["name"], plate_energy=n["energy"])
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
        release_backorders(p["id"])
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
        sid = db().execute("SELECT last_insert_rowid()").fetchone()[0]
        db().execute("UPDATE sales SET status=? WHERE id=?", (initial_status(p["id"]), sid))
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
    data = dash(request.args.get("from") or "0000-00-00", request.args.get("to") or "9999-99-99")
    data["goal"] = goal_info()
    return jsonify(data)


def goal_info():
    s = settings()
    target = num(s.get("goal_amount"), 2000)
    net = dash("0000-00-00", "9999-99-99")["totals"]["net"]
    first = one("""SELECT MIN(d) AS d FROM (SELECT MIN(date) AS d FROM sales UNION ALL SELECT MIN(date) FROM batches
                   UNION ALL SELECT MIN(date) FROM expenses)""")["d"]
    days = 0
    per_day = 0.0
    eta = None
    if first:
        days = max((datetime.now() - datetime.strptime(first, "%Y-%m-%d")).days, 14)
        per_day = net / days
        if net < target and per_day > 0:
            from datetime import timedelta
            eta = (datetime.now() + timedelta(days=int((target - net) / per_day) + 1)).strftime("%Y-%m-%d")
    return {"target": target, "net": round(net, 2), "pct": round(max(min(net / target * 100, 100), 0), 1) if target else 0,
            "per_day": round(per_day, 2), "eta": eta, "since": first}


def dash(f, t):
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
    return dict(
        totals={**{k: round(v, 2) for k, v in tot.items()}, "gross_profit": round(gross_profit, 2),
                "waste": round(waste, 2), "expenses": round(exp_total, 2), "net": round(net, 2),
                "spent_printing": round(spent_printing, 2), "stock_value": round(stock_value, 2),
                "stock_units": sum(max(p["stock"], 0) for p in ps)},
        months=sorted([{k: (round(v, 2) if k != "month" else v) for k, v in m.items()} for m in months.values()], key=lambda x: x["month"]),
        products=sorted([{k: (round(v, 2) if k != "product" else v) for k, v in p.items()} for p in prods.values()], key=lambda x: -x["profit"]),
        low_stock=low,
        expense_categories=[dict(category=k, amount=round(v, 2)) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
    )


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
ETSY_SCOPES = "transactions_r transactions_w shops_r listings_r listings_w"


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
                    var_text = "; ".join(f"{v.get('formatted_name')}: {v.get('formatted_value')}" for v in (t.get("variations") or []) if v.get("formatted_value"))
                    dup = one("SELECT id,status,variation FROM sales WHERE order_ref=? AND product_id=? AND (variation=? OR variation='' OR ?='')", (order, p["id"], var_text, var_text))
                    if dup:
                        skipped += 1
                        first = False
                        upd = []
                        if r.get("is_shipped") and dup["status"] != "shipped":
                            db().execute("UPDATE sales SET status='shipped' WHERE id=?", (dup["id"],))
                        if var_text and not dup["variation"]:
                            db().execute("UPDATE sales SET variation=? WHERE id=?", (var_text, dup["id"]))
                        continue
                    d = dict(qty=t.get("quantity") or 1, unit_price=money_of(t.get("price")),
                             shipping_charged=money_of(r.get("total_shipping_cost")) if first else 0,
                             discount=money_of(r.get("discount_amt")) if first else 0)
                    first = False
                    qty, price, ship, disc, offsite, fees = sale_values(d, p, s)
                    db().execute(
                        """INSERT INTO sales(date,order_ref,product_id,qty,unit_price,shipping_charged,discount,postage_cost,
                           offsite_ad,fees,unit_cost,source,variation,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (date, order, p["id"], qty, price, ship, disc, 0, 0, fees, unit_cost_of(p), "etsy-api", var_text, "shipped"))
                    sid = db().execute("SELECT last_insert_rowid()").fetchone()[0]
                    if not r.get("is_shipped"):
                        db().execute("UPDATE sales SET status=? WHERE id=?", (initial_status(p["id"]), sid))
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


# ---------------------------------------------------------------- actual Etsy label costs (from the payment ledger)
LABEL_WORDS = ("label", "postage", "shipping", "royal mail", "evri", "hermes")


def fetch_label_entries(days=90):
    s = settings()
    now = int(time.time())
    out, offset = [], 0
    while offset < 1000:
        d = etsy_get(f"/shops/{s['etsy_shop_id']}/payment-account/ledger-entries",
                     {"limit": 100, "offset": offset, "min_created": now - 86400 * days, "max_created": now})
        res = d.get("results", [])
        out += res
        offset += 100
        if len(res) < 100:
            break
    found = []
    for e in out:
        text = " ".join(str(e.get(k) or "") for k in ("description", "ledger_type", "reference_type")).lower()
        if (e.get("amount") or 0) < 0 and any(w in text for w in LABEL_WORDS):
            found.append({"entry_id": e.get("entry_id"), "date": datetime.utcfromtimestamp(e.get("created_timestamp") or e.get("create_date") or 0).strftime("%Y-%m-%d"),
                          "amount": round(abs(e["amount"]) / 100.0, 2), "description": e.get("description") or "", "type": e.get("ledger_type") or "",
                          "reference_type": e.get("reference_type") or "", "reference_id": str(e.get("reference_id") or "")})
    return found, len(out)


@app.route("/api/etsy/label-costs", methods=["GET", "POST"])
def api_label_costs():
    """GET: label charges found in the Etsy ledger and which sale they match. POST: apply the matches as postage cost."""
    try:
        found, total = fetch_label_entries()
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    for f in found:
        sale = one("SELECT id,order_ref,postage_cost FROM sales WHERE order_ref=? ORDER BY id LIMIT 1", (f["reference_id"],)) if f["reference_id"] else None
        f["sale_id"] = sale["id"] if sale else None
        f["order_ref"] = sale["order_ref"] if sale else ""
        f["current_postage"] = sale["postage_cost"] if sale else None
    if request.method == "POST":
        applied = 0
        for f in found:
            if f["sale_id"] and not f["current_postage"]:
                db().execute("UPDATE sales SET postage_cost=? WHERE id=?", (f["amount"], f["sale_id"]))
                f["current_postage"] = f["amount"]
                applied += 1
        db().commit()
        return jsonify({"applied": applied, "found": found, "ledger_entries": total})
    return jsonify({"found": found, "ledger_entries": total})


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


@app.route("/api/etsy/shipping-costs")
def api_etsy_ship_costs():
    """What each Etsy shipping profile charges the buyer for UK delivery (fixed-price profiles only)."""
    shop = settings().get("etsy_shop_id")
    try:
        d = etsy_get(f"/shops/{shop}/shipping-profiles")
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    out = {}
    for r in d.get("results", []):
        dests = r.get("shipping_profile_destinations") or []
        dom = [x for x in dests if x.get("destination_country_iso") == (r.get("origin_country_iso") or "GB")] or \
              [x for x in dests if x.get("destination_country_iso") in (None, "")] or dests
        cost = None
        for x in dom:
            pc = x.get("primary_cost")
            if isinstance(pc, dict) and pc.get("amount") is not None:
                cost = round(pc["amount"] / (pc.get("divisor") or 100), 2)
                break
        if cost is not None:
            out[str(r["shipping_profile_id"])] = {"title": r.get("title", ""), "cost": cost}
    return jsonify(out)


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


@app.route("/api/etsy/return-policies")
def api_etsy_returns():
    shop = settings().get("etsy_shop_id")
    try:
        d = etsy_get(f"/shops/{shop}/policies/return")
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    out = []
    for r in d.get("results", []):
        t = ("Accepts returns" if r.get("accepts_returns") else "No returns") + (", exchanges" if r.get("accepts_exchanges") else "")
        if r.get("accepts_returns") and r.get("return_deadline"):
            t += f" · {r['return_deadline']} days"
        out.append({"id": r["return_policy_id"], "title": t})
    return jsonify(out)


@app.route("/api/etsy/shop-sections")
def api_etsy_sections():
    shop = settings().get("etsy_shop_id")
    try:
        d = etsy_get(f"/shops/{shop}/sections")
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify([{"id": r["shop_section_id"], "title": r.get("title", "")} for r in d.get("results", [])])


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


SYN = {"holder": ["holder", "organizer", "organiser", "rack", "storage"], "mount": ["mount", "wall", "hanger", "bracket", "hook"],
       "rack": ["rack", "storage", "organizer"], "dock": ["dock", "holder", "stand", "storage"], "stand": ["stand", "holder"],
       "battery": ["battery", "storage", "organizer"], "tool": ["tool", "storage", "organizer"], "drill": ["tool", "storage"],
       "wall": ["wall", "mount", "hanger"], "organiser": ["organizer", "storage"], "organizer": ["organizer", "storage"],
       "charger": ["charger", "holder", "stand"], "bit": ["tool", "storage"], "workshop": ["tool", "storage"], "garage": ["storage", "tool"]}
STOP = {"for", "the", "and", "with", "of", "a", "an", "to", "in", "on", "by", "your", "3d", "printed", "print", "custom", "personalised",
        "personalized", "gift", "wall", "set", "pack", "size", "x", "pcs", "piece", "pieces", "fits", "compatible", "new"}


def stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


@app.route("/api/etsy/taxonomy-suggest")
def api_taxonomy_suggest():
    text = (request.args.get("q") or "").lower()
    words = [stem(w) for w in re.findall(r"[a-z]+", text)]
    words = [w for w in words if w not in STOP and len(w) > 1]
    if not words:
        return jsonify([])
    try:
        nodes = load_taxonomy()
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    weights = {}
    for w in words:
        weights[w] = max(weights.get(w, 0), 3.0)
        for syn in SYN.get(w, []):
            weights[stem(syn)] = max(weights.get(stem(syn), 0), 1.0)
    scored = []
    for n in nodes:
        segs = [stem(x) for x in re.findall(r"[a-z]+", n["path"].lower())]
        last = set(stem(x) for x in re.findall(r"[a-z]+", n["path"].lower().split(" › ")[-1]))
        sc = 0.0
        for w, wt in weights.items():
            if w in last:
                sc += wt * 2
            elif w in segs:
                sc += wt
        if sc:
            # prefer shorter, more specific-but-not-niche leaves; penalise clothing/jewellery/etc. noise slightly
            sc -= len(segs) * 0.02
            scored.append((sc, n))
    scored.sort(key=lambda x: -x[0])
    return jsonify([n for _, n in scored[:8]])


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
                    l_var_name=?,l_var_options=?,l_personalisation=?,l_ship_profile=?,l_readiness=?,l_return_policy=?,l_shop_section=? WHERE id=?""",
                 (d.get("l_title", "")[:140], d.get("l_desc", ""), d.get("l_tags", ""), d.get("l_materials", ""),
                  int(num(d.get("l_qty"))), d.get("taxonomy_id") or None, d.get("taxonomy_name", ""),
                  d.get("l_var_name", ""), d.get("l_var_options", ""), d.get("l_personalisation", ""),
                  str(d.get("l_ship_profile") or ""), str(d.get("l_readiness") or ""),
                  str(d.get("l_return_policy") or ""), str(d.get("l_shop_section") or ""), pid))
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


def listing_inputs(s, p):
    """Everything about the listing that comes from our app (shared by create-draft and update)."""
    ship_profile = p["l_ship_profile"] or s.get("listing_shipping_profile_id")
    readiness = p["l_readiness"] or s.get("listing_readiness_state_id")
    problems = []
    if not p["taxonomy_id"]:
        problems.append("choose a category")
    if num(p["price"]) <= 0:
        problems.append("set a price on the product")
    if not ship_profile:
        problems.append("choose a shipping profile (here, or a default in Settings)")
    title = (p["l_title"] or p["name"]).strip()[:140]
    desc = p["l_desc"] or (s.get("listing_desc_template") or "{name}").replace("{name}", p["name"])
    tags = clean_list((p["l_tags"] or "") + "," + (s.get("listing_default_tags") or ""), 20, 13)
    mats = clean_list(p["l_materials"] or s.get("listing_materials"), 45, 13)
    qty = int(p["l_qty"] or num(s.get("listing_quantity"), 1)) or 1
    form = {"title": title, "description": desc, "who_made": s.get("listing_who_made") or "i_did",
            "when_made": s.get("listing_when_made") or "made_to_order", "taxonomy_id": p["taxonomy_id"],
            "shipping_profile_id": ship_profile, "type": "physical", "is_supply": "false",
            "should_auto_renew": "true" if s.get("listing_auto_renew") == "1" else "false"}
    if tags:
        form["tags"] = ",".join(tags)
    if mats:
        form["materials"] = ",".join(mats)
    per_item_g = num(p["plate_grams"]) / max(p["items_per_plate"] or 1, 1)
    if per_item_g > 0:
        form["item_weight"] = round(per_item_g, 1)
        form["item_weight_unit"] = "g"
    if num(p["item_l"]) > 0 and num(p["item_w"]) > 0 and num(p["item_h"]) > 0:
        form.update({"item_length": p["item_l"], "item_width": p["item_w"], "item_height": p["item_h"], "item_dimensions_unit": "cm"})
    rp = p["l_return_policy"] or s.get("listing_return_policy_id")
    if rp:
        form["return_policy_id"] = rp
    sec = p["l_shop_section"] or s.get("listing_shop_section_id")
    if sec:
        form["shop_section_id"] = sec
    return form, problems, qty, readiness


def upload_missing_images(s, lid, pid):
    warnings, n = [], 0
    for im in rows("SELECT * FROM product_images WHERE product_id=? AND etsy_image_id IS NULL ORDER BY rank,id", (pid,)):
        path = os.path.join(IMG_DIR, im["filename"])
        try:
            ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
            r = etsy_send("POST", f"/shops/{s['etsy_shop_id']}/listings/{lid}/images",
                          fields={"rank": str(im["rank"] if im["rank"] > 0 else 1)},
                          files={"image": (im["filename"], open(path, "rb").read(), ctype)})
            db().execute("UPDATE product_images SET etsy_image_id=? WHERE id=?", (r.get("listing_image_id") or 0, im["id"]))
            n += 1
        except (EtsyError, OSError) as e:
            warnings.append("Photo not uploaded: " + str(e))
    db().commit()
    return n, warnings


def colour_code(name, used):
    """Short SKU suffix: initials for multi-word names (Dark Blue -> DB), first 3 letters for one word (Black -> BLA)."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    code = ("".join(w[0] for w in words) if len(words) > 1 else (words[0][:3] if words else "X")).upper()
    base, n = code, 2
    while code in used:
        code = f"{base}{n}"
        n += 1
    used.add(code)
    return code


def push_inventory(s, lid, p, qty, readiness):
    """Set price / quantity / variations on the listing. Returns how many variation options were sent."""
    options = [o.strip() for o in (p["l_var_options"] or "").split("|") if o.strip()]
    vname = re.sub(r"[()]", "", (p["l_var_name"] or "").strip())[:45]
    price = round(float(p["price"]), 2)

    def offering():
        off = {"price": price, "quantity": qty, "is_enabled": True}
        if readiness:
            off["readiness_state_id"] = int(readiness)
        return off

    products, seen, used_codes = [], set(), set()
    if options and vname:
        for o in options:
            o = re.sub(r"[()]", "", o).strip()[:20]
            if not o or o.lower() in seen:
                continue
            seen.add(o.lower())
            prod = {"property_values": [{"property_id": 513, "property_name": vname, "value_ids": [], "values": [o]}], "offerings": [offering()]}
            if p["sku"]:
                prod["sku"] = (p["sku"][:26] + "-" + colour_code(o, used_codes))[:32]
            products.append(prod)
    if not products:
        prod = {"property_values": [], "offerings": [offering()]}
        if p["sku"]:
            prod["sku"] = p["sku"][:32]
        products = [prod]
    # Etsy requires SKUs to match across all variations unless the SKU is declared as varying with the property.
    sku_prop = [513] if (seen and p["sku"]) else []
    etsy_send("PUT", f"/listings/{lid}/inventory", json_body={"products": products, "price_on_property": [], "quantity_on_property": [], "sku_on_property": sku_prop})
    return len(seen)


@app.route("/api/products/<int:pid>/etsy-draft", methods=["POST"])
def api_etsy_draft(pid):
    s = settings()
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    if p["etsy_listing_id"]:
        return jsonify({"error": "A draft already exists for this product — use “Update on Etsy” instead."}), 400
    if "listings_w" not in (s.get("etsy_scopes") or ""):
        return jsonify({"error": "Etsy needs re-authorising with permission to create listings (Settings → Etsy connection)."}), 400
    form, problems, qty, readiness = listing_inputs(s, p)
    if problems:
        return jsonify({"error": "Before creating the draft: " + ", ".join(problems) + "."}), 400
    form.update({"quantity": qty, "price": f"{p['price']:.2f}"})
    if readiness:
        form["readiness_state_id"] = readiness
    try:
        res = etsy_send("POST", f"/shops/{s['etsy_shop_id']}/listings", form=form)
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    lid = res.get("listing_id")
    url = f"https://www.etsy.com/your/shops/me/tools/listings/{lid}"
    db().execute("UPDATE products SET etsy_listing_id=?, etsy_listing_url=? WHERE id=?", (lid, url, pid))
    db().commit()
    n_img, warnings = upload_missing_images(s, lid, pid)
    nvar = 0
    try:
        nvar = push_inventory(s, lid, p, qty, readiness)
    except Exception as e:
        warnings.append("Variations/inventory weren't added: " + str(e))
    return jsonify({"ok": True, "listing_id": lid, "url": url, "warnings": warnings, "photos": n_img,
                    "personalisation": p["l_personalisation"] or "", "variations": nvar})


@app.route("/api/products/<int:pid>/etsy-update", methods=["POST"])
def api_etsy_update(pid):
    """Push the current listing details (text, tags, category, shipping, dimensions, price, stock, variations, new photos) to the existing Etsy listing."""
    s = settings()
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p or not p["etsy_listing_id"]:
        return jsonify({"error": "This product has no Etsy listing yet."}), 400
    if "listings_w" not in (s.get("etsy_scopes") or ""):
        return jsonify({"error": "Etsy needs re-authorising with permission to edit listings (Settings → Etsy connection)."}), 400
    form, problems, qty, readiness = listing_inputs(s, p)
    if problems:
        return jsonify({"error": "Before updating: " + ", ".join(problems) + "."}), 400
    lid = p["etsy_listing_id"]
    form.pop("type", None)
    warnings = []
    try:
        etsy_send("PATCH", f"/shops/{s['etsy_shop_id']}/listings/{lid}", form=form)
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    nvar = 0
    try:
        nvar = push_inventory(s, lid, p, qty, readiness)
    except Exception as e:
        warnings.append("Price/stock/variations weren't updated: " + str(e))
    n_img, w2 = upload_missing_images(s, lid, pid)
    return jsonify({"ok": True, "warnings": warnings + w2, "photos": n_img, "variations": nvar})


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



@app.route("/api/bambuddy/spools")
def api_bb_spools():
    s = settings()
    try:
        data = as_list(bb_get("/inventory/spools"))
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    out = []
    for sp in data:
        if sp.get("archived_at"):
            continue
        label = num(sp.get("label_weight"), 1000)
        used = num(sp.get("weight_used"))
        remaining = max(label - used, 0)
        cpk = sp.get("cost_per_kg")
        cpk = num(cpk) if cpk not in (None, "") else num(s.get("default_cost_per_kg"))
        out.append({"id": sp.get("id"), "brand": sp.get("brand") or "", "material": sp.get("material") or "",
                    "subtype": sp.get("subtype") or "", "colour": sp.get("color_name") or "", "rgba": sp.get("rgba") or "",
                    "label_g": round(label), "remaining_g": round(remaining), "pct": round(remaining / label * 100) if label else 0,
                    "cost_per_kg": round(cpk, 2), "value": round(remaining / 1000 * cpk, 2)})
    return jsonify(sorted(out, key=lambda x: (x["material"], x["colour"].lower())))

BASIC_COLOURS = {"Black": (0, 0, 0), "White": (255, 255, 255), "Grey": (128, 128, 128), "Silver": (192, 192, 192),
                 "Red": (220, 30, 30), "Dark Red": (130, 20, 20), "Orange": (255, 130, 20), "Yellow": (250, 230, 30),
                 "Gold": (212, 175, 55), "Green": (30, 170, 60), "Dark Green": (20, 90, 40), "Lime": (180, 220, 40),
                 "Teal": (0, 160, 160), "Cyan": (0, 170, 230), "Light Blue": (150, 190, 240), "Blue": (30, 70, 200),
                 "Navy": (20, 30, 110), "Purple": (130, 60, 180), "Lilac": (190, 160, 220), "Pink": (240, 130, 180),
                 "Magenta": (230, 0, 130), "Brown": (130, 75, 45), "Beige": (225, 205, 170), "Cream": (245, 235, 210)}


def colour_name_from_rgba(rgba):
    try:
        r, g, b = (int(rgba[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, TypeError):
        return ""
    return min(BASIC_COLOURS, key=lambda n: sum((x - y) ** 2 for x, y in zip(BASIC_COLOURS[n], (r, g, b))))


def material_ok(sp_material, wanted):
    """wanted: comma list of families, '' or 'all' = everything. 'PLA' matches PLA, PLA Matte, PLA Silk…"""
    fams = [w.strip().lower() for w in (wanted or "").split(",") if w.strip()]
    if not fams or "all" in fams:
        return True
    m = (sp_material or "").strip().lower()
    return any(m == f or m.split(" ")[0] == f for f in fams)


@app.route("/api/bambuddy/colours")
def api_bb_colours():
    """Filament colours you currently have (non-archived spools), grouped by colour name.
    Spools without a colour name get one guessed from their hex colour."""
    material = request.args.get("material", "")
    s = settings()
    try:
        spools = as_list(bb_get("/inventory/spools"))
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    min_g = num(s.get("colour_min_remaining_g"), 50)
    out = {}
    for sp in spools:
        if sp.get("archived_at") or not material_ok(sp.get("material"), material):
            continue
        name = (sp.get("color_name") or "").strip() or colour_name_from_rgba(sp.get("rgba") or "")
        if not name:
            continue
        remaining = max(num(sp.get("label_weight"), 1000) - num(sp.get("weight_used")), 0)
        c = out.setdefault(name.lower(), {"name": name, "grams": 0.0, "spools": 0, "rgba": sp.get("rgba") or ""})
        c["grams"] += remaining
        c["spools"] += 1
    res = [dict(c, grams=round(c["grams"]), in_stock=c["grams"] >= min_g) for c in out.values()]
    return jsonify(sorted(res, key=lambda c: (-c["in_stock"], c["name"].lower())))


@app.route("/api/bambuddy/materials")
def api_bb_materials():
    try:
        spools = as_list(bb_get("/inventory/spools"))
    except BBError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(sorted({(sp.get("material") or "").split(" ")[0] for sp in spools if not sp.get("archived_at") and sp.get("material")}))


# ---------------------------------------------------------------- order pipeline
PIPE = ["to_print", "printing", "to_ship", "shipped"]


@app.route("/api/orders")
def api_orders():
    ls = rows("""SELECT s.id,s.date,s.order_ref,s.product_id,s.qty,s.status,s.variation,s.tracking,s.shipped_at,s.notes,
                 (s.unit_price*s.qty + s.shipping_charged - s.discount) AS revenue, p.name AS product
                 FROM sales s JOIN products p ON p.id=s.product_id ORDER BY s.date DESC, s.id DESC""")
    orders = {}
    for l in ls:
        key = l["order_ref"] or f"sale-{l['id']}"
        o = orders.setdefault(key, {"key": key, "order_ref": l["order_ref"], "date": l["date"], "items": [], "revenue": 0.0,
                                    "tracking": "", "shipped_at": ""})
        o["items"].append(l)
        o["revenue"] = round(o["revenue"] + l["revenue"], 2)
        o["tracking"] = o["tracking"] or l["tracking"]
        o["shipped_at"] = o["shipped_at"] or l["shipped_at"]
    out = []
    for o in orders.values():
        o["status"] = min((i["status"] for i in o["items"]), key=lambda x: PIPE.index(x) if x in PIPE else 3)
        out.append(o)
    return jsonify(out)


@app.route("/api/orders/status", methods=["POST"])
def api_orders_status():
    d = request.get_json(force=True)
    st = d.get("status")
    if st not in PIPE:
        return jsonify({"error": "bad status"}), 400
    for sid in d.get("sale_ids", []):
        db().execute("UPDATE sales SET status=? WHERE id=?", (st, int(sid)))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/orders/ship", methods=["POST"])
def api_orders_ship():
    d = request.get_json(force=True)
    ids = [int(x) for x in d.get("sale_ids", [])]
    if not ids:
        return jsonify({"error": "no items"}), 400
    s = settings()
    first = one("SELECT order_ref FROM sales WHERE id=?", (ids[0],))
    warning = None
    if d.get("push") and first and (first["order_ref"] or "").isdigit():
        try:
            body = {"tracking_code": d.get("tracking_code", ""), "carrier_name": d.get("carrier", ""), "send_bcc": False}
            if d.get("note"):
                body["note_to_buyer"] = d["note"]
            etsy_send("POST", f"/shops/{s['etsy_shop_id']}/receipts/{first['order_ref']}/tracking", json_body=body)
        except EtsyError as e:
            warning = "Marked shipped here, but Etsy didn't accept the tracking: " + str(e)
    stamp = today()
    for sid in ids:
        db().execute("UPDATE sales SET status='shipped', tracking=?, shipped_at=? WHERE id=?", (d.get("tracking_code", ""), stamp, sid))
    pc = num(d.get("postage_cost"))
    if pc > 0:
        db().execute("UPDATE sales SET postage_cost=? WHERE id=?", (pc, ids[0]))
        for sid in ids[1:]:
            db().execute("UPDATE sales SET postage_cost=0 WHERE id=? AND postage_cost=0", (sid,))
    db().commit()
    return jsonify({"ok": True, "warning": warning})


@app.route("/api/etsy/carriers")
def api_etsy_carriers():
    try:
        s = settings()
        d = http_json(ETSY_API + "/shipping-carriers?origin_country_iso=GB", headers=etsy_headers(s))
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(sorted({c["name"] for c in d.get("results", []) if c.get("name")}))


# ---------------------------------------------------------------- listing sync
@app.route("/api/etsy/listing-sync", methods=["POST"])
def api_listing_sync():
    """Pull state/views/favourites/quantity for every listing in the shop and attach them to linked products.
    Unlinked listings whose title matches a product name are linked automatically."""
    s = settings()
    try:
        found, offset = [], 0
        for state in ("active", "draft", "inactive"):
            offset = 0
            while True:
                d = etsy_get(f"/shops/{s['etsy_shop_id']}/listings", {"state": state, "limit": 100, "offset": offset})
                res = d.get("results", [])
                found += res
                offset += 100
                if len(res) < 100:
                    break
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    prods = rows("SELECT id,name,etsy_listing_id FROM products")
    by_id = {p["etsy_listing_id"]: p for p in prods if p["etsy_listing_id"]}
    linked = updated = 0
    for l in found:
        p = by_id.get(l["listing_id"])
        if not p:
            t = (l.get("title") or "").strip().lower()
            p = next((x for x in prods if not x["etsy_listing_id"] and x["name"].strip().lower() and x["name"].strip().lower() in t), None)
            if p:
                db().execute("UPDATE products SET etsy_listing_id=?, etsy_listing_url=? WHERE id=?",
                             (l["listing_id"], l.get("url") or "", p["id"]))
                p["etsy_listing_id"] = l["listing_id"]
                linked += 1
        if p:
            db().execute("UPDATE products SET etsy_state=?, etsy_views=?, etsy_favs=?, etsy_qty=?, etsy_synced=? WHERE id=?",
                         (l.get("state", ""), l.get("views") or 0, l.get("num_favorers") or 0, l.get("quantity"), today(), p["id"]))
            updated += 1
    db().commit()
    return jsonify({"ok": True, "listings": len(found), "updated": updated, "linked": linked})


@app.route("/api/products/<int:pid>/push-stock", methods=["POST"])
def api_push_stock(pid):
    """Set the Etsy listing quantity to our stock (simple listings only; variation listings are managed in inventory)."""
    s = settings()
    p = one("SELECT * FROM products WHERE id=?", (pid,))
    if not p or not p["etsy_listing_id"]:
        return jsonify({"error": "This product isn't linked to an Etsy listing."}), 400
    qty = max(1, int(request.get_json(force=True).get("qty") or stock_of(pid)))
    try:
        etsy_send("PATCH", f"/shops/{s['etsy_shop_id']}/listings/{p['etsy_listing_id']}", form={"quantity": qty})
    except EtsyError as e:
        return jsonify({"error": str(e)}), 502
    db().execute("UPDATE products SET etsy_qty=? WHERE id=?", (qty, pid))
    db().commit()
    return jsonify({"ok": True, "qty": qty})


# ---------------------------------------------------------------- shipping calculator
# Prices are what YOU pay (Royal Mail 1st/2nd stamp prices are VAT-exempt; Tracked/Evri include VAT).
# Seeded from published rates (Royal Mail parcel prices from 5 Oct 2026); Tracked and Evri are approximate - editable in the app.
SHIP_DEFAULT = [
    {"name": "Royal Mail 2nd Class Large Letter", "dims": [35.3, 25, 2.5], "tracked": False, "tiers": [[100, 1.55], [250, 1.90], [500, 2.40], [750, 2.70]]},
    {"name": "Royal Mail 1st Class Large Letter", "dims": [35.3, 25, 2.5], "tracked": False, "tiers": [[100, 3.30], [750, 3.60]]},
    {"name": "Royal Mail 2nd Class Small Parcel", "dims": [45, 35, 16], "tracked": False, "tiers": [[2000, 4.60]]},
    {"name": "Royal Mail 1st Class Small Parcel", "dims": [45, 35, 16], "tracked": False, "tiers": [[2000, 5.90]]},
    {"name": "Royal Mail 2nd Class Medium Parcel", "dims": [61, 46, 46], "tracked": False, "tiers": [[2000, 7.50], [10000, 9.50], [20000, 14.20]]},
    {"name": "Royal Mail 1st Class Medium Parcel", "dims": [61, 46, 46], "tracked": False, "tiers": [[2000, 8.70], [10000, 10.90], [20000, 16.90]]},
    {"name": "Royal Mail Tracked 48 Large Letter (est.)", "etsy": True, "dims": [35.3, 25, 2.5], "tracked": True, "tiers": [[1000, 2.85]]},
    {"name": "Royal Mail Tracked 48 Small Parcel (est.)", "etsy": True, "dims": [45, 35, 16], "tracked": True, "tiers": [[2000, 3.65]]},
    {"name": "Royal Mail Tracked 48 Medium Parcel (est.)", "etsy": True, "dims": [61, 46, 46], "tracked": True, "tiers": [[2000, 5.55], [10000, 7.35], [20000, 11.85]]},
    {"name": "Royal Mail Tracked 24 Small Parcel (est.)", "etsy": True, "dims": [45, 35, 16], "tracked": True, "tiers": [[2000, 4.65]]},
    {"name": "Royal Mail Tracked 24 Large Letter (est.)", "etsy": True, "dims": [35.3, 25, 2.5], "tracked": True, "tiers": [[1000, 3.80]]},
    {"name": "Royal Mail Tracked 24 Medium Parcel (est.)", "etsy": True, "dims": [61, 46, 46], "tracked": True, "tiers": [[2000, 6.55], [10000, 8.55], [20000, 13.85]]},
    {"name": "Evri ParcelShop drop-off (est.)", "etsy": True, "dims": [120, 60, 60], "tracked": True, "tiers": [[1000, 2.94], [2000, 4.37], [5000, 6.59], [10000, 6.68], [15000, 10.28]]},
]


def ship_rates():
    raw = settings().get("ship_rates") or ""
    try:
        return json.loads(raw) if raw else SHIP_DEFAULT
    except ValueError:
        return SHIP_DEFAULT


@app.route("/api/shipping/rates", methods=["GET", "PUT", "DELETE"])
def api_ship_rates():
    if request.method == "PUT":
        d = request.get_json(force=True)
        db().execute("INSERT INTO settings(key,value) VALUES('ship_rates',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(d),))
        db().commit()
    elif request.method == "DELETE":
        db().execute("DELETE FROM settings WHERE key='ship_rates'")
        db().commit()
    return jsonify(ship_rates())


@app.route("/api/products/<int:pid>/size", methods=["POST"])
def api_product_size(pid):
    d = request.get_json(force=True)
    db().execute("UPDATE products SET item_l=?,item_w=?,item_h=? WHERE id=?", (num(d.get("item_l")), num(d.get("item_w")), num(d.get("item_h")), pid))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>/shipping", methods=["POST"])
def api_product_shipping(pid):
    d = request.get_json(force=True)
    db().execute("UPDATE products SET pack_g=?,pack_l=?,pack_w=?,pack_h=?,postage_cost=? WHERE id=?",
                 (num(d.get("pack_g")), num(d.get("pack_l")), num(d.get("pack_w")), num(d.get("pack_h")), num(d.get("postage_cost")), pid))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- supplies (packaging etc.)
# Stock is derived: counted stock minus what sales have used since that count, so editing/deleting a sale self-corrects.
def supply_usage(sid, since_id=0, since_date=None):
    q = """SELECT COALESCE(SUM(s.qty*ps.qty),0) AS u FROM sales s JOIN product_supplies ps ON ps.product_id=s.product_id
           WHERE ps.supply_id=? AND s.id>?"""
    args = [sid, since_id]
    if since_date:
        q += " AND s.date>=?"
        args.append(since_date)
    u = one(q, tuple(args))["u"]
    q2 = "SELECT COUNT(DISTINCT COALESCE(NULLIF(order_ref,''),'s'||id)) AS n FROM sales WHERE id>?"
    args2 = [since_id]
    if since_date:
        q2 += " AND date>=?"
        args2.append(since_date)
    return u, one(q2, tuple(args2))["n"]


def supply_rows():
    out = []
    t = datetime.strptime(today(), "%Y-%m-%d")
    for s in rows("SELECT * FROM supplies ORDER BY active DESC, name COLLATE NOCASE"):
        u, n = supply_usage(s["id"], s["counted_sale_id"] or 0)
        used = u + n * (s["per_order"] or 0)
        remaining = round((s["stock"] or 0) - used, 2)
        d30 = (t - timedelta(days=30)).strftime("%Y-%m-%d")
        u30, n30 = supply_usage(s["id"], 0, d30)
        first = (one("SELECT MIN(date) AS d FROM sales WHERE date>=?", (d30,)) or {}).get("d")
        window = max(7, min(30, (t - datetime.strptime(first, "%Y-%m-%d")).days + 1)) if first else 30
        rate = max((u30 + n30 * (s["per_order"] or 0)) / window, (s["manual_week"] or 0) / 7.0)
        days_left = round(remaining / rate) if rate > 0 and remaining > 0 else (0 if remaining <= 0 and rate > 0 else None)
        reorder_by, status = "", "ok"
        if days_left is not None:
            reorder_by = (t + timedelta(days=days_left - (s["lead_days"] or 0))).strftime("%Y-%m-%d")
            gap = days_left - (s["lead_days"] or 0)
            status = "order_now" if gap <= 0 else "soon" if gap <= 7 else "ok"
        elif remaining <= 0:
            status = "order_now"
        out.append(dict(s, remaining=remaining, rate_per_day=round(rate, 3), days_left=days_left, reorder_by=reorder_by, status=status))
    return out


@app.route("/api/supplies", methods=["GET", "POST"])
def api_supplies():
    if request.method == "POST":
        d = request.get_json(force=True)
        if not (d.get("name") or "").strip():
            return jsonify({"error": "Give it a name."}), 400
        mx = one("SELECT COALESCE(MAX(id),0) AS m FROM sales")["m"]
        db().execute("""INSERT INTO supplies(name,link,unit_cost,stock,counted_sale_id,counted_at,lead_days,per_order,manual_week,notes,box_l,box_w,box_h,box_g)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (d["name"].strip(), d.get("link", ""), num(d.get("unit_cost")), num(d.get("stock")), mx, today(),
                      int(num(d.get("lead_days"))), num(d.get("per_order")), num(d.get("manual_week")), d.get("notes", ""),
                      num(d.get("box_l")), num(d.get("box_w")), num(d.get("box_h")), num(d.get("box_g"))))
        db().commit()
        return jsonify({"ok": True})
    return jsonify(supply_rows())


@app.route("/api/supplies/<int:sid>", methods=["PUT", "DELETE"])
def api_supply(sid):
    if request.method == "DELETE":
        db().execute("DELETE FROM product_supplies WHERE supply_id=?", (sid,))
        db().execute("DELETE FROM supplies WHERE id=?", (sid,))
        db().commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    db().execute("""UPDATE supplies SET name=?,link=?,unit_cost=?,lead_days=?,per_order=?,manual_week=?,notes=?,active=?,box_l=?,box_w=?,box_h=?,box_g=? WHERE id=?""",
                 (d.get("name", "").strip(), d.get("link", ""), num(d.get("unit_cost")), int(num(d.get("lead_days"))),
                  num(d.get("per_order")), num(d.get("manual_week")), d.get("notes", ""), 1 if d.get("active", True) else 0,
                  num(d.get("box_l")), num(d.get("box_w")), num(d.get("box_h")), num(d.get("box_g")), sid))
    if d.get("stock") not in (None, ""):
        cur = next((r for r in supply_rows() if r["id"] == sid), None)
        if cur and abs(cur["remaining"] - num(d["stock"])) > 1e-9:
            mx = one("SELECT COALESCE(MAX(id),0) AS m FROM sales")["m"]
            db().execute("UPDATE supplies SET stock=?, counted_sale_id=?, counted_at=? WHERE id=?", (num(d["stock"]), mx, today(), sid))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/supplies/<int:sid>/restock", methods=["POST"])
def api_supply_restock(sid):
    d = request.get_json(force=True)
    cur = next((r for r in supply_rows() if r["id"] == sid), None)
    if not cur:
        return jsonify({"error": "not found"}), 404
    qty = num(d.get("qty"))
    new = max(cur["remaining"], 0) + qty if d.get("mode") != "set" else qty
    mx = one("SELECT COALESCE(MAX(id),0) AS m FROM sales")["m"]
    db().execute("UPDATE supplies SET stock=?, counted_sale_id=?, counted_at=? WHERE id=?", (new, mx, today(), sid))
    cost = num(d.get("cost"))
    if d.get("mode") != "set" and cost > 0:
        db().execute("INSERT INTO expenses(date,category,description,amount) VALUES(?,?,?,?)",
                     (today(), "Packaging", f"Restock: {cur['name']} x{qty:g}", cost))
        if qty > 0:
            db().execute("UPDATE supplies SET unit_cost=? WHERE id=?", (round(cost / qty, 4), sid))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/supplies/receive", methods=["POST"])
def api_supplies_receive():
    """One-shot 'order arrived': adds stock to every line, updates unit costs, logs ONE expense."""
    d = request.get_json(force=True)
    lines = [l for l in (d.get("lines") or []) if num(l.get("qty")) > 0]
    if not lines:
        return jsonify({"error": "Enter a quantity on at least one line"}), 400
    delivery = num(d.get("delivery"))
    goods = sum(num(l.get("cost")) for l in lines)
    mx = one("SELECT COALESCE(MAX(id),0) AS m FROM sales")["m"]
    cur = {r["id"]: r for r in supply_rows()}
    names = []
    for l in lines:
        qty, cost = num(l.get("qty")), num(l.get("cost"))
        sid = l.get("supply_id")
        if sid and int(sid) in cur:
            sid = int(sid)
            new = max(cur[sid]["remaining"], 0) + qty
            nm = cur[sid]["name"]
        else:
            nm = (l.get("name") or "").strip()
            if not nm:
                continue
            sid = db().execute("INSERT INTO supplies(name,link,unit_cost,stock,counted_sale_id,counted_at) VALUES(?,?,?,?,?,?)",
                               (nm, (l.get("link") or "").strip(), 0, 0, mx, today())).lastrowid
            new = qty
        db().execute("UPDATE supplies SET stock=?, counted_sale_id=?, counted_at=? WHERE id=?", (new, mx, today(), sid))
        if cost > 0:
            landed = cost + (delivery * cost / goods if goods > 0 else 0)  # share delivery across lines by value
            db().execute("UPDATE supplies SET unit_cost=? WHERE id=?", (round(landed / qty, 4), sid))
        names.append("%s x%g" % (nm, qty))
    total = round(goods + delivery, 2)
    if total > 0:
        desc = (d.get("note") or "").strip()
        desc = (desc + ": " if desc else "Supplies order: ") + ", ".join(names)
        db().execute("INSERT INTO expenses(date,category,description,amount) VALUES(?,?,?,?)",
                     (d.get("date") or today(), "Packaging", desc[:300], total))
    db().commit()
    return jsonify({"ok": True, "lines": len(names), "expense": total})


@app.route("/api/products/<int:pid>/supplies", methods=["GET", "PUT"])
def api_product_supplies(pid):
    if request.method == "PUT":
        db().execute("DELETE FROM product_supplies WHERE product_id=?", (pid,))
        for r in request.get_json(force=True):
            if r.get("supply_id") and num(r.get("qty")) > 0:
                db().execute("INSERT OR REPLACE INTO product_supplies(product_id,supply_id,qty) VALUES(?,?,?)", (pid, int(r["supply_id"]), num(r["qty"])))
        db().commit()
    return jsonify(rows("SELECT supply_id,qty FROM product_supplies WHERE product_id=?", (pid,)))


# ---------------------------------------------------------------- product files (3D models etc.)
@app.route("/api/files")
def api_files_overview():
    out = []
    for p in rows("SELECT id,name,sku,active,model_url,archive_name FROM products ORDER BY active DESC, name COLLATE NOCASE"):
        p["files"] = rows("SELECT id,orig_name,size,added_at FROM product_files WHERE product_id=? ORDER BY id", (p["id"],))
        p["images"] = rows("SELECT id,rank FROM product_images WHERE product_id=? ORDER BY rank,id", (p["id"],))
        out.append(p)
    return jsonify(out)


@app.route("/api/products/<int:pid>/files", methods=["POST"])
def api_product_files_add(pid):
    os.makedirs(FILE_DIR, exist_ok=True)
    n = 0
    for f in request.files.getlist("files"):
        orig = os.path.basename(f.filename or "file")
        name = uuid.uuid4().hex + os.path.splitext(orig)[1].lower()
        path = os.path.join(FILE_DIR, name)
        f.save(path)
        db().execute("INSERT INTO product_files(product_id,filename,orig_name,size) VALUES(?,?,?,?)", (pid, name, orig, os.path.getsize(path)))
        n += 1
    db().commit()
    return jsonify({"ok": True, "added": n})


@app.route("/api/files/<int:fid>/download")
def api_file_download(fid):
    r = one("SELECT filename,orig_name FROM product_files WHERE id=?", (fid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(FILE_DIR, r["filename"], as_attachment=True, download_name=r["orig_name"])


@app.route("/api/files/<int:fid>", methods=["DELETE"])
def api_file_delete(fid):
    r = one("SELECT filename FROM product_files WHERE id=?", (fid,))
    if r:
        try:
            os.remove(os.path.join(FILE_DIR, r["filename"]))
        except OSError:
            pass
        db().execute("DELETE FROM product_files WHERE id=?", (fid,))
        db().commit()
    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>/model-url", methods=["POST"])
def api_model_url(pid):
    db().execute("UPDATE products SET model_url=? WHERE id=?", ((request.get_json(force=True).get("url") or "").strip(), pid))
    db().commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- measurements database
def _save_measure(iid, d):
    c = db()
    vals = [v for v in (d.get("values") or []) if (v.get("label") or "").strip()]
    if iid is None:
        iid = c.execute("INSERT INTO measure_items(name,category,product_id,notes) VALUES(?,?,?,?)",
                        ((d.get("name") or "Untitled").strip(), (d.get("category") or "").strip(),
                         d.get("product_id") or None, d.get("notes") or "")).lastrowid
    else:
        c.execute("UPDATE measure_items SET name=?,category=?,product_id=?,notes=?,updated_at=datetime('now') WHERE id=?",
                  ((d.get("name") or "Untitled").strip(), (d.get("category") or "").strip(),
                   d.get("product_id") or None, d.get("notes") or "", iid))
        c.execute("DELETE FROM measure_values WHERE item_id=?", (iid,))
    for i, v in enumerate(vals):
        c.execute("INSERT INTO measure_values(item_id,label,value,unit,rank) VALUES(?,?,?,?,?)",
                  (iid, v["label"].strip(), str(v.get("value", "")).strip(), (v.get("unit") or "").strip(), i))
    c.commit()
    return iid


@app.route("/api/measurements", methods=["GET", "POST"])
def api_measurements():
    if request.method == "POST":
        return jsonify({"ok": True, "id": _save_measure(None, request.get_json(force=True))})
    out = rows("SELECT * FROM measure_items ORDER BY category COLLATE NOCASE, name COLLATE NOCASE")
    for m in out:
        m["values"] = rows("SELECT label,value,unit FROM measure_values WHERE item_id=? ORDER BY rank,id", (m["id"],))
    return jsonify(out)


@app.route("/api/measurements/<int:iid>", methods=["PUT", "DELETE"])
def api_measurement(iid):
    if request.method == "DELETE":
        db().execute("DELETE FROM measure_values WHERE item_id=?", (iid,))
        db().execute("DELETE FROM measure_items WHERE id=?", (iid,))
        db().commit()
        return jsonify({"ok": True})
    _save_measure(iid, request.get_json(force=True))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- GitHub backup
import threading
import gzip

BACKUP_LOCK = threading.Lock()
BACKUP_MAX = 45 * 1024 * 1024  # GitHub contents API is unreliable above ~50MB


def _gh(method, repo, token, path, body=None):
    req = urllib.request.Request("https://api.github.com/repos/" + repo + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "User-Agent": "etsy-tracker-backup", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError("GitHub %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))


def _blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _db_snapshot():
    src = sqlite3.connect(DB_PATH)
    tmp = DB_PATH + ".bak"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    for k in ("backup_token", "etsy_access_token", "etsy_refresh_token", "etsy_oauth_verifier", "etsy_oauth_state"):
        dst.execute("UPDATE settings SET value='' WHERE key=?", (k,))
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    with open(tmp, "rb") as f:
        data = gzip.compress(f.read(), 9, mtime=0)
    os.remove(tmp)
    return data


def run_backup():
    """Incremental backup of the database, photos and 3D files to a GitHub repo. Returns a status string."""
    if not BACKUP_LOCK.acquire(blocking=False):
        return "A backup is already running"
    con = sqlite3.connect(DB_PATH)
    try:
        s = {r[0]: r[1] for r in con.execute("SELECT key,value FROM settings")}
        repo, token, branch = (s.get("backup_repo") or "").strip(), (s.get("backup_token") or "").strip(), (s.get("backup_branch") or "main").strip()
        if not repo or not token:
            raise RuntimeError("Set the repository and token first")
        info = _gh("GET", repo, token, "")
        if not info.get("private"):
            raise RuntimeError("Repository is public — your data would be exposed. Use a private repo.")
        # existing blob shas on the branch
        have = {}
        try:
            tree = _gh("GET", repo, token, "/git/trees/%s?recursive=1" % urllib.parse.quote(branch))
            have = {t["path"]: t["sha"] for t in tree.get("tree", []) if t["type"] == "blob"}
        except RuntimeError as e:
            if "404" not in str(e) and "409" not in str(e):
                raise
        items = [("etsy-tracker/etsy.db.gz", _db_snapshot())]
        for folder, name in ((IMG_DIR, "images"), (FILE_DIR, "files")):
            if os.path.isdir(folder):
                for fn in sorted(os.listdir(folder)):
                    p = os.path.join(folder, fn)
                    if os.path.isfile(p):
                        items.append(("etsy-tracker/%s/%s" % (name, fn), p))
        sent = skipped = big = 0
        for path, data in items:
            if isinstance(data, str):
                if os.path.getsize(data) > BACKUP_MAX:
                    big += 1
                    continue
                with open(data, "rb") as f:
                    data = f.read()
            if have.get(path) == _blob_sha(data):
                skipped += 1
                continue
            body = {"message": "Backup " + datetime.now().strftime("%Y-%m-%d %H:%M"), "branch": branch,
                    "content": base64.b64encode(data).decode()}
            if path in have:
                body["sha"] = have[path]
            _gh("PUT", repo, token, "/contents/" + urllib.parse.quote(path), body)
            sent += 1
        msg = "OK %s — %d uploaded, %d unchanged%s" % (datetime.now().strftime("%d %b %H:%M"), sent, skipped,
                                                      (", %d too big (>45MB) skipped" % big) if big else "")
    except Exception as e:
        msg = "FAILED %s — %s" % (datetime.now().strftime("%d %b %H:%M"), e)
    finally:
        BACKUP_LOCK.release()
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('backup_status',?)", (msg,))
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('backup_last',?)", (str(int(time.time())),))
    con.commit()
    con.close()
    return msg


def _backup_loop():
    while True:
        time.sleep(600)
        try:
            con = sqlite3.connect(DB_PATH)
            s = {r[0]: r[1] for r in con.execute("SELECT key,value FROM settings")}
            con.close()
            if s.get("backup_enabled") == "1":
                every = max(1.0, float(s.get("backup_every_hours") or 24)) * 3600
                if time.time() - float(s.get("backup_last") or 0) >= every:
                    run_backup()
        except Exception:
            pass


@app.route("/api/backup/github", methods=["POST"])
def api_backup_github():
    msg = run_backup()
    return jsonify({"ok": msg.startswith("OK"), "status": msg})


def seed_measurements():
    """One-time starter checklist of items to measure (values left blank for the user to fill in)."""
    con = sqlite3.connect(DB_PATH)
    done = con.execute("SELECT value FROM settings WHERE key='measure_seeded'").fetchone()
    if not done:
        PHOTO = ""
        batt = [("Rail head width (widest part of the T)", "mm"), ("Rail neck width (narrow part under the head)", "mm"),
                ("Rail head thickness", "mm"), ("Neck height (seating face to underside of head)", "mm"),
                ("Rail length in slide direction", "mm"), ("Which end goes in first / lead-in taper", ""),
                ("Battery body width", "mm"), ("Battery body length", "mm"), ("Battery body height", "mm"),
                ("Latch button size", "mm"), ("Latch distance from front of rail", "mm"), ("Latch distance from side edge", "mm"),
                ("Lock notch position", "mm"), ("Lock notch depth", "mm"), ("Battery weight", "g"), ("Capacity (2.0 / 4.0 / 5.0 Ah)", "Ah"),
                ("Photo: rail from the front", PHOTO), ("Photo: rail from the side", PHOTO), ("Photo: underside", PHOTO)]
        chg = [("Length", "mm"), ("Width", "mm"), ("Height", "mm"), ("Cable exit: which face", ""), ("Cable exit: distance from edges", "mm"),
               ("Cable diameter", "mm"), ("Wall-mount slot spacing", "mm"), ("Wall-mount slot width", "mm"), ("Wall-mount slot depth", "mm"),
               ("Battery bay opening (keep clear)", "mm"), ("Feet / vents that must stay clear", ""),
               ("Photo: front", PHOTO), ("Photo: back", PHOTO), ("Photo: underside", PHOTO), ("Photo: side", PHOTO)]
        port = [("Port slot head width", "mm"), ("Port slot neck width", "mm"), ("Port slot depth", "mm"), ("Port length in slide direction", "mm"),
                ("Latch position inside the port", "mm"), ("Tool body hang below the port (wall mount clearance)", "mm"),
                ("Tool length", "mm"), ("Tool foot width", "mm"), ("Tool weight", "g"),
                ("Photo: port from below", PHOTO), ("Photo: tool from the side", PHOTO), ("Photo: tool from the front", PHOTO)]
        hose = [("Hose end outside diameter at tip", "mm"), ("Hose end inside diameter at tip", "mm"), ("Hose end outside diameter 30 mm back", "mm"),
                ("Hose end inside diameter 30 mm back", "mm"), ("Taper", "")]
        inlet = [("Vac inlet outside diameter", "mm"), ("Hose fit (twists on / pushes on / clips)", "")]
        dport = [("Dust port outside diameter", "mm"), ("Dust port inside diameter", "mm"), ("Dust port length", "mm"), ("Dust port taper", ""),
                 ("Lug / slot / flat on the port", "")]
        items = []
        for b in ("Einhell PXC", "Titan TXP", "Erbauer / Evolution EXT"):
            items.append((b + " battery", "1. Batteries", batt))
        for b in ("Einhell PXC", "Titan TXP", "Erbauer / Evolution EXT"):
            for k in ("single", "dual"):
                items.append((b + " charger (" + k + ")", "2. Chargers", chg))
        for n in ("Cheapest Titan tool", "Cheapest Erbauer tool", "Einhell drill", "Einhell impact driver"):
            items.append((n + " — battery port", "3. Tool battery ports", port))
        items += [("Vac hose end (one per vac)", "4. Dust extraction", hose), ("Vac inlet (one per vac)", "4. Dust extraction", inlet),
                  ("Tool dust port (one per tool)", "4. Dust extraction", dport),
                  ("Wall & screws", "5. Mounting", [("Screws you normally use", ""), ("Wall material", "")])]
        for name, cat, vals in items:
            iid = con.execute("INSERT INTO measure_items(name,category,notes) VALUES(?,?,?)", (name, cat, "")).lastrowid
            for i, (l, u) in enumerate(vals):
                con.execute("INSERT INTO measure_values(item_id,label,value,unit,rank) VALUES(?,?,?,?,?)", (iid, l, "", u, i))
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('measure_seeded','1')")
        con.commit()
    con.close()


init_db()
seed_measurements()
threading.Thread(target=_backup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5055)))
