"""PostyBirb login import (4.69.0, spec 033 phase 8): read-only, no secrets in the scan, right keys."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import pb_import
from database import accounts as adb
from database.db import get_connection

_LOCAL = ("127.0.0.1", 50000)


def _cookie_db(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB)")
    c.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", rows)
    c.commit()
    c.close()


@pytest.fixture
def pb(tmp_path, monkeypatch):
    """A PostyBirb 3 install with placeholder accounts and fake tokens."""
    docs, app = tmp_path / "PostyBirb", tmp_path / "postybirb-plus"
    (docs / "data").mkdir(parents=True)
    recs = [
        {"_id": "bsky-1", "website": "Bluesky", "alias": "Inkwolf", "data": {"username": "inkwolf.example.social", "password": "fake-app-pass"}},
        {"_id": "fa-1", "website": "FurAffinity", "alias": "Penwright"},
        {"_id": "ib-1", "website": "Inkbunny", "alias": "ThirdFur", "data": {"username": "ThirdFur", "sid": "fake-sid"}},
        {"_id": "sf-1", "website": "SoFurry", "alias": "SecondHandle"},
        {"_id": "gone", "website": "Bluesky", "alias": "Old"},
        {"_id": "gone", "$$deleted": True},
    ]
    (docs / "data" / "accounts.db").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    _cookie_db(app / "Partitions" / "fa-1" / "Network" / "Cookies",
               [(".furaffinity.net", "a", "fake-a", b""), (".furaffinity.net", "b", "fake-b", b""),
                (".doubleclick.net", "x", "ad", b"")])
    monkeypatch.setenv("PAWPOLLER_PB_DIR", str(docs))
    monkeypatch.setenv("PAWPOLLER_PB_APPDATA", str(app))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for d in (docs, app) for p in d.rglob("*") if p.is_file()}
    made_before = {a["account_id"] for a in adb.list_accounts(_conn())}
    yield tmp_path
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for d in (docs, app) for p in d.rglob("*") if p.is_file()}
    assert after == before, "PostyBirb's files must never change"
    conn = _conn()
    for a in adb.list_accounts(conn):
        if a["account_id"] not in made_before:
            config.delete_settings_keys([config.account_setting_key(a["account_id"], f, bool(a["is_default"]))
                                         for f in config.PLATFORM_CREDENTIAL_FIELDS.get(a["platform"], [])])
            conn.execute("DELETE FROM session_cache WHERE account_id = ?", (a["account_id"],))
            conn.execute("DELETE FROM accounts WHERE account_id = ?", (a["account_id"],))
    conn.commit()
    conn.close()
    config.delete_settings_keys(["pb_import_asked"])


def _conn():
    return get_connection()


def test_the_scan_has_no_secrets(pb):
    r = pb_import.scan()
    assert r["found"] and r["version"] == 3
    by = {a["pb_id"]: a for a in r["accounts"]}
    assert set(by) == {"bsky-1", "fa-1", "ib-1", "sf-1"}          # the deleted one is gone
    assert by["bsky-1"]["portable"] and by["fa-1"]["portable"] and by["ib-1"]["portable"]
    assert not by["sf-1"]["portable"] and "API token" in by["sf-1"]["reason"]
    text = json.dumps(r)
    for secret in ("fake-app-pass", "fake-a", "fake-b", "fake-sid"):
        assert secret not in text


def test_apply_writes_the_right_keys(pb):
    res = {x["pb_id"]: x for x in pb_import.apply(["bsky-1", "fa-1", "ib-1", "sf-1"])}
    assert res["sf-1"]["status"] == "failed"
    s = config.get_settings()

    def creds(pid):
        conn = _conn()
        try:
            a = adb.get_account(conn, res[pid]["account_id"])
        finally:
            conn.close()
        return config.resolve_account_credentials(a["platform"], a["account_id"], bool(a["is_default"]), s)
    assert creds("bsky-1")["bsky_identifier"] == "inkwolf.example.social"
    assert creds("bsky-1")["bsky_app_password"] == "fake-app-pass"
    assert (creds("fa-1")["fa_cookie_a"], creds("fa-1")["fa_cookie_b"]) == ("fake-a", "fake-b")
    conn = _conn()
    try:
        from database import queries
        assert queries.get_cached_session(conn, res["ib-1"]["account_id"])["sid"] == "fake-sid"
    finally:
        conn.close()
    assert s.get("pb_import_asked") is True


def test_duplicates_are_skipped(pb):
    pb_import.apply(["bsky-1"])
    again = pb_import.apply(["bsky-1"])
    assert again[0]["status"] == "skipped"
    assert next(a for a in pb_import.scan()["accounts"] if a["pb_id"] == "bsky-1")["reason"] == "Already in PawPoller."


def test_an_account_with_a_login_is_never_overwritten(pb):
    conn = _conn()
    try:
        default = adb.get_default_account_id(conn, "fa", create=True)
        d = adb.get_account(conn, default)
    finally:
        conn.close()
    key = config.account_setting_key(default, "fa_cookie_a", bool(d["is_default"]))
    old = config.get_settings().get(key)
    config.save_settings({key: old or "keep-me"})
    try:
        res = pb_import.apply(["fa-1"])[0]
        assert res["account_id"] != default
        assert config.get_settings().get(key) == (old or "keep-me")
    finally:
        if not old:
            config.delete_settings_keys([key])


def test_a_locked_file_says_so(pb, monkeypatch):
    def _locked(*a, **k):
        raise PermissionError
    monkeypatch.setattr(pb_import.shutil, "copyfile", _locked)
    r = pb_import.scan()
    assert r.get("locked") and "Close PostyBirb" in r["note"]


def test_encrypted_cookies_are_refused(pb):
    p = pb / "postybirb-plus" / "Partitions" / "fa-1" / "Network" / "Cookies"
    p.unlink()
    _cookie_db(p, [(".furaffinity.net", "a", "", b"v10xxxx"), (".furaffinity.net", "b", "", b"v10yyyy")])
    # (the fixture's hash check compares against the original files, so restore them after)
    fa = next(a for a in pb_import.scan()["accounts"] if a["pb_id"] == "fa-1")
    assert not fa["portable"] and "encrypted" in fa["reason"]
    p.unlink()
    _cookie_db(p, [(".furaffinity.net", "a", "fake-a", b""), (".furaffinity.net", "b", "fake-b", b""),
                   (".doubleclick.net", "x", "ad", b"")])


def test_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_PB_DIR", str(tmp_path / "nothing"))
    assert pb_import.scan() == {"found": False, "accounts": []}


def test_server_mode_is_a_404(pb, monkeypatch):
    import dashboard
    import posting.scheduler as sch
    monkeypatch.setattr(sch, "detect_runtime_mode", lambda: "server")
    assert TestClient(dashboard.app, client=_LOCAL).get("/api/pb-import/scan").status_code == 404


def test_desktop_route_scans(pb, monkeypatch):
    import dashboard
    import posting.scheduler as sch
    monkeypatch.setattr(sch, "detect_runtime_mode", lambda: "desktop")
    c = TestClient(dashboard.app, client=_LOCAL)
    assert c.get("/api/pb-import/scan").json()["found"]
    assert TestClient(dashboard.app, client=("203.0.113.9", 1)).get("/api/pb-import/scan").status_code in (401, 403, 404)
