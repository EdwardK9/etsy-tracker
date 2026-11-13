"""Fees, costs and profit: the numbers the dashboard is built from."""
import pytest

import app as tracker
from conftest import product, sales

RATES = {"fee_transaction_pct": "6.5", "fee_processing_pct": "4", "fee_regulatory_pct": "0.32", "fee_processing_fixed": "0.20",
         "fee_listing_per_item": "0.15", "fee_offsite_ads_pct": "15", "fee_vat_pct": "0"}


def test_fees_are_percentage_of_gross_plus_fixed_parts():
    # 10.82% of £20 = 2.164, + 0.20 processing + 2 x 0.15 listing
    assert tracker.calc_fees(20, 2, False, RATES) == 2.66


def test_offsite_ads_add_their_percentage():
    assert tracker.calc_fees(20, 1, True, RATES) == pytest.approx(2.164 + 0.35 + 3.0, abs=0.006)


def test_vat_applies_to_the_whole_fee():
    assert tracker.calc_fees(20, 1, False, {**RATES, "fee_vat_pct": "20"}) == round((2.164 + 0.35) * 1.2, 2)


def test_num_reads_currency_text():
    assert tracker.num("£1,234.50") == 1234.5
    assert tracker.num("") == 0
    assert tracker.num("abc", 7) == 7


def test_unit_cost_spreads_the_plate_and_adds_extras(client, make_product):
    pid = make_product(plate_cost=6, plate_energy=0.4, items_per_plate=4, extra_cost=0.25)
    assert product(client, pid)["unit_cost"] == pytest.approx(1.85)


def test_pack_of_n_costs_n_items(client, make_product):
    pid = make_product(plate_cost=6, items_per_plate=4, pack_qty=3)
    assert product(client, pid)["unit_cost"] == pytest.approx(4.5)


def test_labour_is_charged_at_the_hourly_rate(client, make_product):
    client.put("/api/settings", json={"labour_rate_per_hour": "12"})
    pid = make_product(plate_cost=0, labour_minutes=10)
    assert product(client, pid)["unit_cost"] == pytest.approx(2.0)


def test_manual_sale_profit(client, make_product):
    pid = make_product(price=10, plate_cost=6, items_per_plate=4)  # 1.50 each
    client.post("/api/sales", json={"product_id": pid, "qty": 2, "shipping_charged": 3, "discount": 1, "postage_cost": 2.5})
    s = sales(client)[0]
    assert s["revenue"] == pytest.approx(22)
    assert s["fees"] == tracker.calc_fees(22, 2, False, RATES)
    assert s["profit"] == pytest.approx(22 - s["fees"] - 2.5 - 3.0)


def test_typed_fees_are_kept(client, make_product):
    pid = make_product()
    client.post("/api/sales", json={"product_id": pid, "fees": "1.11"})
    assert sales(client)[0]["fees"] == 1.11


def test_sale_keeps_the_cost_it_was_made_at(client, make_product):
    pid = make_product(plate_cost=6, items_per_plate=4)
    client.post("/api/sales", json={"product_id": pid})
    client.put(f"/api/products/{pid}", json={"plate_cost": 12})
    assert sales(client)[0]["unit_cost"] == pytest.approx(1.5)
    assert product(client, pid)["unit_cost"] == pytest.approx(3.0)


def test_editing_a_sale_for_a_missing_product_is_rejected(client, make_product):
    pid = make_product()
    client.post("/api/sales", json={"product_id": pid})
    r = client.put(f"/api/sales/{sales(client)[0]['id']}", json={"product_id": 9999, "qty": 1})
    assert r.status_code == 400


def test_moving_a_sale_to_another_product_uses_that_products_cost(client, make_product):
    a = make_product(name="A", plate_cost=4, items_per_plate=4)
    b = make_product(name="B", plate_cost=20, items_per_plate=4)
    client.post("/api/sales", json={"product_id": a})
    client.put(f"/api/sales/{sales(client)[0]['id']}", json={"product_id": b, "qty": 1})
    assert sales(client)[0]["unit_cost"] == pytest.approx(5.0)


def test_dashboard_totals(client, make_product):
    pid = make_product(price=10, plate_cost=6, items_per_plate=4)
    client.post("/api/batches", json={"product_id": pid, "qty_good": 5, "qty_failed": 2, "date": "2026-03-01"})
    client.post("/api/sales", json={"product_id": pid, "qty": 2, "fees": "1", "postage_cost": 2, "date": "2026-03-02", "order_ref": "A"})
    client.post("/api/sales", json={"product_id": pid, "qty": 1, "fees": "1", "date": "2026-04-02", "order_ref": "B"})
    client.post("/api/expenses", json={"amount": 4, "category": "Tools", "date": "2026-03-05"})
    d = client.get("/api/dashboard").get_json()
    t = d["totals"]
    assert (t["revenue"], t["fees"], t["postage"], t["cogs"], t["items"], t["orders"]) == (30, 2, 2, 4.5, 3, 2)
    assert t["waste"] == 3.0            # 2 failed x 1.50
    assert t["net"] == pytest.approx(30 - 2 - 2 - 4.5 - 3 - 4)
    assert t["stock_units"] == 2 and t["stock_value"] == 3.0
    assert [m["month"] for m in d["months"]] == ["2026-03", "2026-04"]
    assert sum(m["profit"] for m in d["months"]) == pytest.approx(t["net"])


def test_dashboard_date_range(client, make_product):
    pid = make_product()
    client.post("/api/sales", json={"product_id": pid, "date": "2026-03-02"})
    client.post("/api/sales", json={"product_id": pid, "date": "2026-04-02"})
    d = client.get("/api/dashboard?from=2026-04-01&to=2026-04-30").get_json()
    assert d["totals"]["items"] == 1


def test_price_and_cost_changes_are_logged(client, make_product):
    pid = make_product(price=10)
    client.put(f"/api/products/{pid}", json={"price": 12})
    client.put(f"/api/products/{pid}", json={"price": 12})  # no change, no new entry
    h = client.get(f"/api/products/{pid}/history").get_json()
    assert [x["value"] for x in h if x["kind"] == "price"] == [10, 12]
    assert len([x for x in h if x["kind"] == "cost"]) == 1
