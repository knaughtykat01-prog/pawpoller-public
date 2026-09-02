"""Masterpiece junk category (2.149.0): kept-but-hidden status.

'junk' hides a Masterpiece from the grid without deleting its folder or members
(for pulled art that isn't wanted — memes, other people's ads, retired pieces).
Reversible: restoring sets status back to ''. Index-only names (no folder) are
junkable too — that's exactly what the swept-in-tweet Masterpieces need.
"""
from fastapi.testclient import TestClient

from database.db import get_connection
from database import masterpiece_queries as mq


def test_set_and_get_status_roundtrip():
    conn = get_connection()
    mq.set_status(conn, "SomePiece", "junk")
    conn.commit()
    assert mq.get_status(conn, "SomePiece") == "junk"
    assert mq.statuses(conn)["SomePiece"] == "junk"
    mq.set_status(conn, "SomePiece", "")
    conn.commit()
    assert mq.get_status(conn, "SomePiece") == ""
    conn.close()


def test_junk_keeps_members_intact():
    conn = get_connection()
    mq.add_member(conn, "KeptPiece", "fa", "123")
    mq.set_status(conn, "KeptPiece", "junk")
    conn.commit()
    assert mq.member_pairs(conn, "KeptPiece") == [("fa", "123")]
    conn.close()


def _client():
    from dashboard import app
    return TestClient(app)


def test_status_endpoint_junks_index_only_name(auth_client=None):
    client = _client()
    conn = get_connection()
    mq.ensure_indexed(conn, "IndexOnly")     # no folder on disk
    conn.commit()
    conn.close()
    r = client.post("/api/masterpieces/IndexOnly/status", json={"status": "junk"})
    assert r.status_code == 200
    assert r.json()["junk"] is True
    conn = get_connection()
    assert mq.get_status(conn, "IndexOnly") == "junk"
    conn.close()
    # restore
    r = client.post("/api/masterpieces/IndexOnly/status", json={"status": ""})
    assert r.status_code == 200
    assert r.json()["junk"] is False


def test_status_endpoint_rejects_bad_status_and_unknown_name():
    client = _client()
    r = client.post("/api/masterpieces/Whatever/status", json={"status": "banana"})
    assert r.status_code == 400
    r = client.post("/api/masterpieces/NoSuchNameAnywhere/status", json={"status": "junk"})
    assert r.status_code == 404
