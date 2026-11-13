"""Etsy order sync and CSV import, with Etsy itself faked."""
import io

import pytest

import app as tracker
from conftest import product, sales


def receipt(rid, lines, shipped=False, ts=1767268800, shipping=350, discount=0):
    return {"receipt_id": rid, "created_timestamp": ts, "is_paid": True, "is_shipped": shipped,
            "total_shipping_cost": {"amount": shipping, "divisor": 100}, "discount_amt": {"amount": discount, "divisor": 100},
            "transactions": [{"title": t, "sku": sku, "quantity": q, "price": {"amount": pr, "divisor": 100},
                              "variations": [{"formatted_name": k, "formatted_value": v} for k, v in var.items()]}
                             for t, sku, q, pr, var in lines]}


@pytest.fixture
def etsy(client, monkeypatch):
    """Connects the shop and lets a test set the receipts Etsy will return."""
    with tracker.app.app_context():
        tracker.set_setting("etsy_shop_id", "1")
        tracker.db().commit()
    box = {"receipts": [], "error": None}

    def fake_get(path, params=None):
        if box["error"]:
            raise tracker.EtsyError(box["error"])
        return {"results": box["receipts"]}
    monkeypatch.setattr(tracker, "etsy_get", fake_get)
    return box


def test_sync_creates_sales_and_charges_shipping_once(client, etsy, make_product):
    a = make_product(name="Holder", sku="H1", plate_cost=6, items_per_plate=4)
    etsy["receipts"] = [receipt(500, [("Holder listing title", "H1", 2, 1000, {}), ("Brand new thing", "", 1, 400, {})])]
    r = client.post("/api/etsy/sync", json={}).get_json()
    assert (r["added"], r["products_created"]) == (2, 1)
    rows = sorted(sales(client), key=lambda s: s["id"])
    assert rows[0]["product_id"] == a and rows[0]["qty"] == 2 and rows[0]["unit_price"] == 10
    assert [x["shipping_charged"] for x in rows] == [3.5, 0]
    assert rows[0]["unit_cost"] == pytest.approx(1.5)
    assert {x["order_ref"] for x in rows} == {"500"}


def test_syncing_twice_adds_nothing(client, etsy, make_product):
    make_product(name="Holder", sku="H1")
    etsy["receipts"] = [receipt(500, [("Holder", "H1", 1, 1000, {"Colour": "Red"}), ("Holder", "H1", 1, 1000, {"Colour": "Blue"})])]
    assert client.post("/api/etsy/sync", json={}).get_json()["added"] == 2
    again = client.post("/api/etsy/sync", json={"full": True}).get_json()
    assert (again["added"], again["skipped_duplicates"]) == (0, 2)
    assert len(sales(client)) == 2


def test_sync_status_follows_stock_and_etsy(client, etsy, make_product):
    pid = make_product(name="Holder", sku="H1")
    client.post("/api/batches", json={"product_id": pid, "qty_good": 1})
    etsy["receipts"] = [receipt(1, [("Holder", "H1", 1, 1000, {})], ts=1767268800),
                        receipt(2, [("Holder", "H1", 1, 1000, {})], ts=1767268900),
                        receipt(3, [("Holder", "H1", 1, 1000, {})], shipped=True, ts=1767269000)]
    client.post("/api/etsy/sync", json={})
    assert {s["order_ref"]: s["status"] for s in sales(client)} == {"1": "to_ship", "2": "to_print", "3": "shipped"}
    etsy["receipts"][0]["is_shipped"] = True       # shipped on Etsy since the last sync
    client.post("/api/etsy/sync", json={})
    assert {s["order_ref"]: s["status"] for s in sales(client)}["1"] == "shipped"


def test_sync_pack_option_uses_more_stock_and_cost(client, etsy, make_product):
    pid = make_product(name="Holder", sku="H1", plate_cost=6, items_per_plate=4, var_map=[{"match": "Quantity: 4", "mult": 4}])
    client.post("/api/batches", json={"product_id": pid, "qty_good": 10})
    etsy["receipts"] = [receipt(9, [("Holder", "H1", 2, 2500, {"Quantity": "4"})])]
    client.post("/api/etsy/sync", json={})
    s = sales(client)[0]
    assert s["mult"] == 4 and s["unit_cost"] == pytest.approx(6.0)
    assert product(client, pid)["stock"] == 2
    assert s["profit"] == pytest.approx(s["revenue"] - s["fees"] - 12.0)


def test_sync_failure_is_reported(client, etsy):
    etsy["error"] = "Etsy returned HTTP 500"
    r = client.post("/api/etsy/sync", json={})
    assert r.status_code == 502 and "500" in r.get_json()["error"]


def test_autosync_records_a_failure_as_failed(client, etsy):
    with tracker.app.app_context():
        tracker.set_setting("etsy_refresh_token", "x")
        tracker.db().commit()
    etsy["error"] = "Etsy returned HTTP 500"
    tracker.autosync_once()
    with tracker.app.app_context():
        assert tracker.settings()["etsy_autosync_status"].startswith("FAILED")
    etsy["error"] = None
    with tracker.app.app_context():
        tracker.set_setting("etsy_autosync_last", "0")
        tracker.db().commit()
    tracker.autosync_once()
    with tracker.app.app_context():
        assert tracker.settings()["etsy_autosync_status"].startswith("OK")


# ---- CSV import

CSV = """Sale Date,Item Name,Quantity,Price,Discount Amount,Order Shipping,Order ID,Variations,SKU
03/02/26,Holder,2,10.00,1.00,3.50,700,Colour:Red,H1
03/02/26,Holder,1,10.00,0.00,3.50,700,Colour:Blue,H1
03/03/26,Unknown thing,1,4.00,0.00,2.00,701,,
"""


def upload(client, text=CSV):
    return client.post("/api/import/etsy", data={"file": (io.BytesIO(text.encode()), "orders.csv")}).get_json()


def test_csv_import(client, make_product):
    pid = make_product(name="Holder", sku="H1")
    r = upload(client)
    assert (r["added"], r["products_created"]) == (3, 1)
    rows = sorted(sales(client), key=lambda s: s["id"])
    assert [x["date"] for x in rows] == ["2026-03-02", "2026-03-02", "2026-03-03"]
    assert [x["shipping_charged"] for x in rows] == [3.5, 0, 2.0]   # once per order
    assert [x["variation"] for x in rows[:2]] == ["Colour:Red", "Colour:Blue"]
    assert product(client, pid)["sold"] == 3


def test_csv_import_twice_adds_nothing(client, make_product):
    make_product(name="Holder", sku="H1")
    upload(client)
    r = upload(client)
    assert (r["added"], r["skipped_duplicates"]) == (0, 3)


def test_csv_without_an_item_column_is_explained(client):
    r = client.post("/api/import/etsy", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "x.csv")})
    assert r.status_code == 400 and "item name" in r.get_json()["error"]


# ---- settings

def test_secrets_never_reach_the_browser(client):
    client.put("/api/settings", json={"bambuddy_key": "hunter2", "etsy_secret": "s3", "goal_amount": "500"})
    s = client.get("/api/settings").get_json()
    assert s["bambuddy_key"] == "" and s["bambuddy_key_set"] is True and s["etsy_secret"] == ""
    assert s["goal_amount"] == "500"
    client.put("/api/settings", json={"bambuddy_key": ""})     # blank keeps the saved one
    assert client.get("/api/settings").get_json()["bambuddy_key_set"] is True


def test_internal_settings_cannot_be_set_from_the_browser(client):
    client.put("/api/settings", json={"etsy_shop_id": "999", "not_a_setting": "x"})
    with tracker.app.app_context():
        s = tracker.settings()
    assert s["etsy_shop_id"] == "" and "not_a_setting" not in s
