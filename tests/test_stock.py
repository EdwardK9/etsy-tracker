"""Stock, the order pipeline, Print today and supplies."""
import pytest

from conftest import product, sales


def statuses(client):
    return {s["order_ref"]: s["status"] for s in sales(client)}


def test_stock_is_good_prints_minus_sales(client, make_product):
    pid = make_product()
    client.post("/api/batches", json={"product_id": pid, "qty_good": 10, "qty_failed": 3})
    client.post("/api/sales", json={"product_id": pid, "qty": 4})
    p = product(client, pid)
    assert (p["made"], p["failed"], p["sold"], p["stock"]) == (10, 3, 4, 6)


def test_sale_ships_from_stock_or_waits_for_a_print(client, make_product):
    pid = make_product()
    client.post("/api/batches", json={"product_id": pid, "qty_good": 1})
    client.post("/api/sales", json={"product_id": pid, "order_ref": "A"})
    client.post("/api/sales", json={"product_id": pid, "order_ref": "B"})
    assert statuses(client) == {"A": "to_ship", "B": "to_print"}


def test_printing_releases_the_oldest_waiting_orders_first(client, make_product):
    pid = make_product()
    client.post("/api/sales", json={"product_id": pid, "order_ref": "old", "qty": 2, "date": "2026-01-01"})
    client.post("/api/sales", json={"product_id": pid, "order_ref": "new", "qty": 2, "date": "2026-01-05"})
    client.post("/api/batches", json={"product_id": pid, "qty_good": 3})
    assert statuses(client) == {"old": "to_ship", "new": "to_print"}
    client.post("/api/batches", json={"product_id": pid, "qty_good": 1})
    assert statuses(client) == {"old": "to_ship", "new": "to_ship"}


def test_orders_group_lines_and_take_the_least_advanced_status(client, make_product):
    a, b = make_product(name="A"), make_product(name="B")
    client.post("/api/batches", json={"product_id": a, "qty_good": 1})
    client.post("/api/sales", json={"product_id": a, "order_ref": "77"})
    client.post("/api/sales", json={"product_id": b, "order_ref": "77"})
    orders = client.get("/api/orders").get_json()
    assert len(orders) == 1 and len(orders[0]["items"]) == 2
    assert orders[0]["status"] == "to_print"


def test_marking_shipped_records_tracking_and_postage_once(client, make_product):
    a, b = make_product(name="A"), make_product(name="B")
    client.post("/api/sales", json={"product_id": a, "order_ref": "77"})
    client.post("/api/sales", json={"product_id": b, "order_ref": "77"})
    ids = sorted(s["id"] for s in sales(client))
    client.post("/api/orders/ship", json={"sale_ids": ids, "tracking_code": "TR1", "postage_cost": 3.2})
    rows = sorted(sales(client), key=lambda s: s["id"])
    assert [r["status"] for r in rows] == ["shipped", "shipped"]
    assert [r["postage_cost"] for r in rows] == [3.2, 0]
    assert rows[0]["tracking"] == "TR1"


def test_bad_status_is_rejected(client):
    assert client.post("/api/orders/status", json={"status": "lost", "sale_ids": []}).status_code == 400


def test_product_with_history_cannot_be_deleted(client, make_product):
    pid = make_product()
    client.post("/api/sales", json={"product_id": pid})
    assert client.delete(f"/api/products/{pid}").status_code == 400
    assert client.delete(f"/api/products/{make_product(name='Unused')}").status_code == 200


# ---- Print today

def print_row(client, pid):
    return next(x for x in client.get("/api/print-today").get_json() if x["product_id"] == pid)


def test_print_today_covers_waiting_orders(client, make_product):
    pid = make_product(plate_grams=120, items_per_plate=4, print_minutes=30)
    client.post("/api/sales", json={"product_id": pid, "qty": 5})
    r = print_row(client, pid)
    assert (r["owed"], r["topup"], r["total"]) == (5, 0, 5)
    assert r["plates"] == 2             # 5 items at 4 per plate
    assert r["grams"] == 150            # 30 g each
    assert r["minutes"] == 150          # print time is entered per item


def test_print_today_counts_every_item_in_a_pack(client, make_product):
    pid = make_product(plate_grams=120, items_per_plate=4, print_minutes=30, pack_qty=3)
    client.post("/api/sales", json={"product_id": pid, "qty": 2})   # 2 packs = 6 items
    r = print_row(client, pid)
    assert (r["plates"], r["grams"], r["minutes"]) == (2, 180, 180)


def test_print_today_tops_up_stocked_items(client, make_product):
    pid = make_product(reorder_at=3, restock_to=8)
    client.post("/api/batches", json={"product_id": pid, "qty_good": 2})
    assert print_row(client, pid)["topup"] == 6


def test_print_per_order_items_are_never_topped_up(client, make_product):
    pid = make_product(reorder_at=3, restock_to=8, made_to_order=True)
    assert all(x["product_id"] != pid for x in client.get("/api/print-today").get_json())


# ---- pack options (one listing, several pack sizes)

def test_pack_option_uses_several_items_per_sale(client, make_product, monkeypatch):
    import app as tracker
    pid = make_product(plate_cost=6, items_per_plate=4, var_map=[{"match": "Quantity: 4", "mult": 4, "price": 30}])
    client.post("/api/batches", json={"product_id": pid, "qty_good": 10})
    with tracker.app.app_context():
        p = tracker.one("SELECT * FROM products WHERE id=?", (pid,))
        assert tracker.variant_mult(p, "Colour: Red; Quantity: 4") == 4
        assert tracker.variant_mult(p, "Colour: Red") == 1


def test_adding_a_pack_option_later_fixes_stock_and_cost_of_past_sales(client, make_product):
    import app as tracker
    pid = make_product(plate_cost=6, items_per_plate=4)   # 1.50 each
    client.post("/api/batches", json={"product_id": pid, "qty_good": 10})
    client.post("/api/sales", json={"product_id": pid, "qty": 1})
    with tracker.app.app_context():
        tracker.db().execute("UPDATE sales SET variation='Quantity: 4'")
        tracker.db().commit()
    client.put(f"/api/products/{pid}", json={"var_map": [{"match": "Quantity: 4", "mult": 4, "price": 30}]})
    s = sales(client)[0]
    assert s["mult"] == 4
    assert s["unit_cost"] == pytest.approx(6.0)    # four items went out, so four items of cost
    assert product(client, pid)["stock"] == 6
    # saving again must not multiply the cost a second time
    client.put(f"/api/products/{pid}", json={"notes": "x"})
    assert sales(client)[0]["unit_cost"] == pytest.approx(6.0)


# ---- supplies

def supply(client, name):
    return next(s for s in client.get("/api/supplies").get_json() if s["name"] == name)


def test_supplies_run_down_as_orders_go_out(client, make_product):
    pid = make_product()
    client.post("/api/supplies", json={"name": "Box", "stock": 10, "unit_cost": 0.4})
    client.post("/api/supplies", json={"name": "Label", "stock": 10, "per_order": 1})
    client.put(f"/api/products/{pid}/supplies", json=[{"supply_id": supply(client, "Box")["id"], "qty": 1}])
    client.post("/api/sales", json={"product_id": pid, "qty": 3, "order_ref": "A"})
    client.post("/api/sales", json={"product_id": pid, "qty": 1, "order_ref": "A"})
    assert supply(client, "Box")["remaining"] == 6      # one per item sold
    assert supply(client, "Label")["remaining"] == 9    # one per order
    assert product(client, pid)["pack_cost"] == pytest.approx(0.4)


def test_restock_adds_stock_logs_the_expense_and_updates_unit_cost(client):
    client.post("/api/supplies", json={"name": "Box", "stock": 2})
    sid = supply(client, "Box")["id"]
    client.post(f"/api/supplies/{sid}/restock", json={"qty": 50, "cost": 10})
    b = supply(client, "Box")
    assert b["remaining"] == 52 and b["unit_cost"] == pytest.approx(0.2)
    e = client.get("/api/expenses").get_json()
    assert len(e) == 1 and e[0]["amount"] == 10 and e[0]["category"] == "Packaging"


def test_order_arrived_shares_delivery_by_value(client):
    client.post("/api/supplies", json={"name": "Box", "stock": 0, "pack_size": 25})
    sid = supply(client, "Box")["id"]
    r = client.post("/api/supplies/receive", json={"delivery": 3, "lines": [
        {"supply_id": sid, "qty": 2, "cost": 10}, {"name": "Tape", "qty": 1, "cost": 5}]}).get_json()
    assert r["expense"] == 18
    assert supply(client, "Box")["remaining"] == 50
    assert supply(client, "Box")["unit_cost"] == pytest.approx(12 / 50)
    assert supply(client, "Tape")["unit_cost"] == pytest.approx(6)


def test_pack_option_matches_however_etsy_spaces_it(client, make_product):
    import app as tracker
    pid = make_product(var_map=[{"match": "Quantity: 4", "mult": 4}, {"match": "Quantity: 40", "mult": 40}])
    with tracker.app.app_context():
        p = tracker.one("SELECT * FROM products WHERE id=?", (pid,))
        assert tracker.variant_mult(p, "Quantity:4,Colour:Red") == 4
        assert tracker.variant_mult(p, "Colour: Red; Quantity: 40") == 40
