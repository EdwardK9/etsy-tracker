import os
import sys
import tempfile

# app.py opens its database at import time, so point it at a throwaway file before importing.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="etsy-tracker-tests-"), "import.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app as tracker


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client backed by a fresh, empty database."""
    monkeypatch.setattr(tracker, "DB_PATH", str(tmp_path / "etsy.db"))
    tracker.init_db()
    return tracker.app.test_client()


@pytest.fixture
def make_product(client):
    def _make(**fields):
        body = {"name": "Widget", "price": 10, "plate_cost": 6, "items_per_plate": 4}
        body.update(fields)
        return client.post("/api/products", json=body).get_json()["id"]
    return _make


def product(client, pid):
    return next(p for p in client.get("/api/products").get_json() if p["id"] == pid)


def sales(client):
    return client.get("/api/sales").get_json()
