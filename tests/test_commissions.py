"""Gap wave 5 §4: Commissions client tracker — CRUD + validation + JSON round-trip."""
import json

import pytest

from database.db import get_connection
from database import commissions_queries as cq


def test_create_and_get_roundtrips_deliver_sites():
    conn = get_connection()
    try:
        cid = cq.create_commission(
            conn, client_name="@fox", description="A ref sheet", price=45.5,
            currency="AUD", status="accepted", due_date="2026-08-01",
            artwork_name="Fox_Ref", deliver_sites=["fa", "ib", "bsky"], notes="rush")
        conn.commit()
        row = cq.get_commission(conn, cid)
        assert row["client_name"] == "@fox"
        assert row["price"] == 45.5
        assert row["currency"] == "AUD"
        assert row["status"] == "accepted"
        assert row["artwork_name"] == "Fox_Ref"
        # deliver_sites comes back as a real list, not a JSON string.
        assert row["deliver_sites"] == ["fa", "ib", "bsky"]
    finally:
        conn.close()


def test_bad_status_falls_back_to_quote():
    conn = get_connection()
    try:
        cid = cq.create_commission(conn, client_name="x", status="banana")
        conn.commit()
        assert cq.get_commission(conn, cid)["status"] == "quote"
    finally:
        conn.close()


def test_deliver_sites_accepts_json_string_and_drops_junk():
    conn = get_connection()
    try:
        cid = cq.create_commission(conn, client_name="x",
                                   deliver_sites='["fa", 5, "ib", null]')
        conn.commit()
        # Non-strings dropped; valid codes kept.
        assert cq.get_commission(conn, cid)["deliver_sites"] == ["fa", "ib"]
    finally:
        conn.close()


def test_update_and_status_validation():
    conn = get_connection()
    try:
        cid = cq.create_commission(conn, client_name="x")
        conn.commit()
        cq.update_commission(conn, cid, status="wip", price="99",
                             deliver_sites=["ig"])
        conn.commit()
        row = cq.get_commission(conn, cid)
        assert row["status"] == "wip"
        assert row["price"] == 99.0
        assert row["deliver_sites"] == ["ig"]
        # A bad status is ignored (the field is simply skipped).
        cq.update_commission(conn, cid, status="nope")
        conn.commit()
        assert cq.get_commission(conn, cid)["status"] == "wip"
    finally:
        conn.close()


def test_list_orders_dated_before_undated():
    conn = get_connection()
    try:
        cq.create_commission(conn, client_name="undated")
        cq.create_commission(conn, client_name="late", due_date="2026-12-01")
        cq.create_commission(conn, client_name="soon", due_date="2026-08-01")
        conn.commit()
        names = [c["client_name"] for c in cq.list_commissions(conn)]
        assert names.index("soon") < names.index("late") < names.index("undated")
    finally:
        conn.close()


def test_delete():
    conn = get_connection()
    try:
        cid = cq.create_commission(conn, client_name="gone")
        conn.commit()
        cq.delete_commission(conn, cid)
        conn.commit()
        assert cq.get_commission(conn, cid) is None
    finally:
        conn.close()


# ── API layer (via TestClient) ──────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    import dashboard
    # Caller monkeypatches is_dashboard_auth_required → open instance, middleware
    # passes through; we exercise the router directly.
    return TestClient(dashboard.app)


def test_api_crud_and_validation(monkeypatch):
    import config
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    c = _client()

    # Missing client_name → 400.
    assert c.post("/api/commissions", json={}).status_code == 400
    # Bad status → 400.
    assert c.post("/api/commissions", json={"client_name": "x", "status": "zzz"}).status_code == 400

    # Create.
    r = c.post("/api/commissions", json={"client_name": "@wolf", "price": 30,
                                         "deliver_sites": ["fa", "e621"]})
    assert r.status_code == 200
    cid = r.json()["id"]

    # List includes the status vocabulary + the new row.
    listing = c.get("/api/commissions").json()
    assert "quote" in listing["statuses"]
    assert any(x["id"] == cid for x in listing["commissions"])

    # Patch status.
    assert c.patch(f"/api/commissions/{cid}", json={"status": "paid"}).status_code == 200
    assert c.get(f"/api/commissions/{cid}").json()["status"] == "paid"
    # Bad patch status → 400.
    assert c.patch(f"/api/commissions/{cid}", json={"status": "bad"}).status_code == 400

    # Delete → subsequent GET 404.
    assert c.delete(f"/api/commissions/{cid}").status_code == 200
    assert c.get(f"/api/commissions/{cid}").status_code == 404
