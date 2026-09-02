"""2.188: commission attachments (any file) + archive completed."""
import config
import pytest

from database.db import get_connection
from database import commissions_queries as cq


# ── Archive ─────────────────────────────────────────────────────────

def test_archive_hides_from_default_list():
    conn = get_connection()
    try:
        a = cq.create_commission(conn, client_name="active")
        b = cq.create_commission(conn, client_name="done")
        conn.commit()
        cq.set_archived(conn, b, True)
        conn.commit()
        active = [c["client_name"] for c in cq.list_commissions(conn)]
        archived = [c["client_name"] for c in cq.list_commissions(conn, archived=True)]
        assert active == ["active"]
        assert archived == ["done"]
        assert cq.count_archived(conn) == 1
        # Unarchive restores it to the active list.
        cq.set_archived(conn, b, False)
        conn.commit()
        assert cq.count_archived(conn) == 0
        assert len(cq.list_commissions(conn)) == 2
    finally:
        conn.close()


def test_archived_migration_on_pre_2188_table():
    """Regression: an upgrade DB has `commissions` WITHOUT `archived` (the 2.187
    table). init_db's schema-load must not index a missing column, and the
    migration must add the column + index. The 2.188 deploy crash-looped on
    exactly this ('no such column: archived')."""
    from database.db import get_connection, init_db
    conn = get_connection()
    try:
        # Recreate the pre-2.188 table shape (no `archived`, no archived index).
        conn.executescript("""
            DROP TABLE IF EXISTS commissions;
            CREATE TABLE commissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                price REAL DEFAULT 0,
                currency TEXT DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'quote',
                due_date TEXT DEFAULT '',
                artwork_name TEXT DEFAULT '',
                deliver_sites TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO commissions (client_name) VALUES ('legacy');
        """)
        conn.commit()
    finally:
        conn.close()
    # Re-run init_db (idempotent) — must NOT raise, and must add archived + index.
    init_db()
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(commissions)").fetchall()]
        assert "archived" in cols
        assert conn.execute(
            "SELECT archived FROM commissions WHERE client_name='legacy'").fetchone()["archived"] == 0
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_commissions_archived'").fetchone() is not None
    finally:
        conn.close()


def test_archived_via_update_allowed_set():
    conn = get_connection()
    try:
        cid = cq.create_commission(conn, client_name="x")
        conn.commit()
        cq.update_commission(conn, cid, archived=1)
        conn.commit()
        assert cq.get_commission(conn, cid)["archived"] == 1
    finally:
        conn.close()


# ── Attachments (unit: helpers) ─────────────────────────────────────

def test_safe_name_strips_separators_and_traversal():
    from routes import commissions_api as api
    assert "/" not in api._safe_name("../../etc/passwd")
    assert "\\" not in api._safe_name("a\\b.png")
    assert api._safe_name("") == "file"
    assert api._safe_name("   ") == "file"
    # A normal name is preserved.
    assert api._safe_name("Ref Sheet_v2.png") == "Ref Sheet_v2.png"


def test_resolve_attachment_blocks_traversal(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from routes import commissions_api as api
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(HTTPException) as ei:
        api._resolve_attachment(1, "../../secret")
    assert ei.value.status_code == 400


# ── Attachments (API via TestClient) ────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    import dashboard
    return TestClient(dashboard.app)


def test_upload_list_download_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = _client()

    cid = c.post("/api/commissions", json={"client_name": "@fox"}).json()["id"]

    # Upload a text file + an image.
    up = c.post(f"/api/commissions/{cid}/files",
                files={"file": ("brief.txt", b"draw a fox", "text/plain")})
    assert up.status_code == 200 and up.json()["filename"] == "brief.txt"
    c.post(f"/api/commissions/{cid}/files",
           files={"file": ("ref.png", b"\x89PNG\r\n\x1a\nfake", "image/png")})

    # List returns both, images flagged.
    files = c.get(f"/api/commissions/{cid}/files").json()["files"]
    names = {f["filename"]: f for f in files}
    assert set(names) == {"brief.txt", "ref.png"}
    assert names["ref.png"]["is_image"] is True
    assert names["brief.txt"]["is_image"] is False

    # Download the text file → bytes match, served as an attachment.
    dl = c.get(f"/api/commissions/{cid}/files/brief.txt")
    assert dl.status_code == 200
    assert dl.content == b"draw a fox"
    assert "attachment" in dl.headers.get("content-disposition", "")
    assert dl.headers.get("x-content-type-options") == "nosniff"
    # Image serves inline with an image content-type.
    img = c.get(f"/api/commissions/{cid}/files/ref.png")
    assert img.headers["content-type"] == "image/png"

    # Delete one.
    assert c.delete(f"/api/commissions/{cid}/files/brief.txt").status_code == 200
    remaining = [f["filename"] for f in c.get(f"/api/commissions/{cid}/files").json()["files"]]
    assert remaining == ["ref.png"]


def test_upload_collision_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = _client()
    cid = c.post("/api/commissions", json={"client_name": "x"}).json()["id"]
    n1 = c.post(f"/api/commissions/{cid}/files", files={"file": ("wip.png", b"a", "image/png")}).json()["filename"]
    n2 = c.post(f"/api/commissions/{cid}/files", files={"file": ("wip.png", b"b", "image/png")}).json()["filename"]
    assert n1 == "wip.png"
    assert n2 == "wip (2).png"


def test_oversized_upload_rejected(tmp_path, monkeypatch):
    from routes import commissions_api as api
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api, "_MAX_FILE_BYTES", 8)   # tiny cap for the test
    c = _client()
    cid = c.post("/api/commissions", json={"client_name": "x"}).json()["id"]
    r = c.post(f"/api/commissions/{cid}/files",
               files={"file": ("big.bin", b"0123456789", "application/octet-stream")})
    assert r.status_code == 413


def test_upload_to_missing_commission_404(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = _client()
    r = c.post("/api/commissions/9999/files", files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 404


def test_deleting_commission_removes_files_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from routes import commissions_api as api
    c = _client()
    cid = c.post("/api/commissions", json={"client_name": "x"}).json()["id"]
    c.post(f"/api/commissions/{cid}/files", files={"file": ("a.txt", b"hi", "text/plain")})
    folder = api._files_dir(cid)
    assert folder.is_dir()
    assert c.delete(f"/api/commissions/{cid}").status_code == 200
    assert not folder.exists()
