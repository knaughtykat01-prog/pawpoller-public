"""Spec 060 US2: a PostyBirb folder uploaded to a server or seat.

FR-SEC-002 (allowlisted, capped unpacking), FR-SEC-004 (no API key), FR-SEC-005 (https,
loopback or Tailscale on every step), FR-SEC-006 (thumbnails), FR-SEC-008 (clean-up and
locking). Stand-in data only (pb_fixture); the data folder is a temp folder.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import pb_import
import pb_upload
from routes import pb_import_api
from tests import pb_fixture

HTTPS = dict(base_url="https://testserver", client=("203.0.113.9", 5000))
LAN = dict(base_url="http://testserver", client=("192.168.1.20", 5000))
TAILNET = dict(base_url="http://testserver", client=("100.101.102.103", 5000))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    (tmp_path / "library").mkdir()
    config.save_settings({"artwork_archive_path": str(tmp_path / "library")})
    from posting import artwork_reader
    assert artwork_reader.get_artwork_archive_path() == tmp_path / "library"
    docs, app = pb_fixture.build(tmp_path / "src")
    pb_import_api._JOB.clear()
    pb_import_api._JOB["state"] = "idle"
    a = FastAPI()
    a.include_router(pb_import_api.pb_import_router)
    return {"app": a, "docs": docs, "pbapp": app, "tmp": tmp_path}


def _zip(folder: Path, prefix: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, prefix + p.relative_to(folder).as_posix())
    return buf.getvalue()


def _send(c: TestClient, uid: str, data: bytes) -> str:
    f = c.post(f"/api/pb-import/uploads/{uid}/files", json={"size": len(data)}).json()
    r = c.put(f"/api/pb-import/uploads/{uid}/files/{f['file']}?offset=0", content=data)
    assert r.status_code == 200 and r.json()["received"] == len(data), r.text
    return f["file"]


def _ready(env, *, logins=True) -> tuple[TestClient, str]:
    c = TestClient(env["app"], **HTTPS)
    uid = c.post("/api/pb-import/uploads").json()["uid"]
    _send(c, uid, _zip(env["docs"], "PostyBirb/"))
    if logins:
        _send(c, uid, _zip(env["pbapp"], "postybirb-plus/"))   # a second zip in the same session
    r = c.post(f"/api/pb-import/uploads/{uid}/finish")
    assert r.status_code == 200, r.text
    return c, uid


def _wait():
    import threading
    for _ in range(600):
        if pb_import_api._JOB.get("state") != "running":
            return dict(pb_import_api._JOB)
        time.sleep(0.05)
    for t in threading.enumerate():               # never leave an import running past the test
        if t.name == "pb-library-import":
            t.join(60)
    raise AssertionError("import didn't finish")


def _sessions(env) -> list[Path]:
    r = env["tmp"] / "data" / "pb_upload"
    return list(r.iterdir()) if r.is_dir() else []


def test_full_upload_lists_like_the_desktop_and_import_deletes_it(env):
    c, uid = _ready(env)
    x = env["tmp"] / "data" / "pb_upload" / uid / "x"
    assert (x / "docs" / "data" / "submissions.db").is_file()
    assert (x / "app" / "Partitions" / "fa-1" / "Network" / "Cookies").is_file()
    assert not list(x.rglob("Local Storage")) and not list(x.rglob("*.log"))   # never written (FR-SEC-002)
    assert not list((env["tmp"] / "data" / "pb_upload" / uid).glob("*.zip"))    # the zips aren't kept
    subs = c.get(f"/api/pb-import/uploads/{uid}/submissions").json()
    assert subs["total"] == 11 and {i["id"] for i in subs["items"]} >= {"s1", "s6"}
    logins = c.get(f"/api/pb-import/uploads/{uid}/logins").json()
    assert {a["pb_id"] for a in logins["accounts"]} >= {"fa-1", "fa-friend"}
    assert c.get(f"/api/pb-import/uploads/{uid}/thumb/s1").headers["content-type"] == "image/png"
    assert c.get(f"/api/pb-import/uploads/{uid}/thumb/s7").status_code == 404    # an SVG is never served
    assert c.get(f"/api/pb-import/uploads/{uid}/thumb/..%2Fx").status_code == 404
    assert c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"ids": ["s1", "s3"]}).json()["started"]
    job = _wait()
    assert job["state"] == "done" and {r["status"] for r in job["results"]} == {"added"}
    assert _sessions(env) == []                                                 # gone with its logins


def test_api_key_is_refused_everywhere(env):
    c = TestClient(env["app"], **HTTPS, headers={"Authorization": "Bearer pp_live_x"})
    checked = 0
    for route in env["app"].routes:
        if not getattr(route, "path", "").startswith("/api/pb-import"):
            continue
        path = route.path.replace("{uid}", "a" * 32).replace("{fid}", "1").replace("{sid}", "s1") + "?offset=0&page=0"
        for method in route.methods:
            assert c.request(method, path, json={}).status_code == 403, (method, route.path)
            checked += 1
    assert checked >= 17                           # every pb-import route, desktop and upload


def test_plain_http_refused_on_every_step_but_tailscale_is_fine(env):
    good = TestClient(env["app"], **HTTPS)
    uid = good.post("/api/pb-import/uploads").json()["uid"]
    fid = good.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 10}).json()["file"]
    lan = TestClient(env["app"], **LAN, headers={"X-Forwarded-Proto": "https",
                                                  "X-Forwarded-For": "100.101.102.103"})
    assert lan.post("/api/pb-import/uploads").status_code == 403
    assert lan.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 1}).status_code == 403
    assert lan.put(f"/api/pb-import/uploads/{uid}/files/{fid}?offset=0", content=b"x").status_code == 403
    assert lan.post(f"/api/pb-import/uploads/{uid}/finish").status_code == 403
    assert TestClient(env["app"], **TAILNET).post("/api/pb-import/uploads").status_code == 200
    assert pb_upload.secure("http", "127.0.0.1") and pb_upload.secure("http", "::1")
    assert not pb_upload.secure("http", "100.63.255.255") and not pb_upload.secure("http", "10.0.0.5")


def _bad_zip(env, **caps) -> tuple[TestClient, str]:
    c = TestClient(env["app"], **HTTPS)
    uid = c.post("/api/pb-import/uploads").json()["uid"]
    return c, uid


def test_a_zip_bomb_is_refused_and_nothing_is_left(env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("PostyBirb/data/submissions.db", b"\n")
        z.writestr("PostyBirb/submission/files/boom.png", b"\x00" * (8 * 1024 * 1024))   # ~1000:1
    c, uid = _bad_zip(env)
    _send(c, uid, buf.getvalue())
    r = c.post(f"/api/pb-import/uploads/{uid}/finish")
    assert r.status_code == 400 and "packed too tightly" in r.json()["detail"]
    assert _sessions(env) == []


@pytest.mark.parametrize("cap,value,words", [("MAX_MEMBERS", 2, "too many files"), ("MAX_TOTAL", 100, "more than 4 GB")])
def test_count_and_total_caps(env, monkeypatch, cap, value, words):
    monkeypatch.setattr(pb_upload, cap, value)
    c, uid = _bad_zip(env)
    _send(c, uid, _zip(env["docs"], "PostyBirb/"))
    r = c.post(f"/api/pb-import/uploads/{uid}/finish")
    assert r.status_code == 400 and words in r.json()["detail"]
    assert _sessions(env) == []


def test_sizes_are_counted_as_written(tmp_path):
    with pytest.raises(pb_upload.Refused):
        pb_upload._copy_capped(io.BytesIO(b"x" * 5000), tmp_path / "out", 4096)


def test_a_damaged_member_is_a_plain_refusal(env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("PostyBirb/data/submissions.db", b"x" * 400)
    raw = bytearray(buf.getvalue())
    raw[raw.index(b"x" * 400) + 10] = ord("y")      # the stored bytes no longer match their checksum
    c, uid = _bad_zip(env)
    _send(c, uid, bytes(raw))
    r = c.post(f"/api/pb-import/uploads/{uid}/finish")
    assert r.status_code == 400 and "damaged" in r.json()["detail"] and _sessions(env) == []


def test_not_a_postybirb_folder(env):
    c, uid = _bad_zip(env)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("holiday/photo.png", pb_fixture.PNG)
        z.writestr("../../escape.db", b"x")
    _send(c, uid, buf.getvalue())
    r = c.post(f"/api/pb-import/uploads/{uid}/finish")
    assert r.status_code == 400 and "doesn't look like a PostyBirb folder" in r.json()["detail"]
    assert not (env["tmp"] / "escape.db").exists() and _sessions(env) == []


def test_over_2gb_and_piece_rules(env):
    c = TestClient(env["app"], **HTTPS)
    uid = c.post("/api/pb-import/uploads").json()["uid"]
    assert c.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 3 * 1024 ** 3}).status_code == 413
    fid = c.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 4}).json()["file"]
    r = c.put(f"/api/pb-import/uploads/{uid}/files/{fid}?offset=2", content=b"ab")
    assert r.status_code == 409 and r.json()["detail"]["received"] == 0
    assert c.put(f"/api/pb-import/uploads/{uid}/files/{fid}?offset=0", content=b"abcdef").status_code == 413
    c.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 4})
    assert c.post(f"/api/pb-import/uploads/{uid}/files", json={"size": 4}).status_code == 400   # two at most


def test_new_session_replaces_old_and_idle_one_is_gone(env, monkeypatch):
    c = TestClient(env["app"], **HTTPS)
    first = c.post("/api/pb-import/uploads").json()["uid"]
    second = c.post("/api/pb-import/uploads").json()["uid"]
    assert [p.name for p in _sessions(env)] == [second] and first != second
    monkeypatch.setattr(pb_upload, "IDLE_SECS", -1)
    assert c.post(f"/api/pb-import/uploads/{second}/files", json={"size": 4}).status_code == 404
    assert _sessions(env) == []


def test_sweep_deletes_idle_and_everything_at_startup(env):
    c = TestClient(env["app"], **HTTPS)
    c.post("/api/pb-import/uploads")
    assert pb_upload.sweep() == 0
    assert pb_upload.sweep(now=time.time() + 31 * 60) == 1
    c.post("/api/pb-import/uploads")
    assert pb_upload.sweep(everything=True) == 1 and _sessions(env) == []


def test_a_failed_import_still_deletes_and_delete_during_import_is_busy(env, monkeypatch):
    c, uid = _ready(env, logins=False)
    pb_upload.LOCK.acquire()
    try:
        assert c.delete(f"/api/pb-import/uploads/{uid}").status_code == 409
        assert c.post("/api/pb-import/uploads").status_code == 409
        assert pb_upload.sweep(everything=True) == 0
    finally:
        pb_upload.LOCK.release()

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(pb_import, "apply_submissions", boom)
    c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"all": True})
    assert _wait()["state"] == "failed"
    assert _sessions(env) == [] and not pb_upload.LOCK.locked()


def test_upload_logins_use_fixed_keys_only(env):
    c, uid = _ready(env)
    before = set(json.loads(config.SETTINGS_PATH.read_text("utf-8")))
    r = c.post(f"/api/pb-import/uploads/{uid}/logins/apply", json={"ids": ["fa-1"]}).json()
    assert r["results"][0]["status"] == "imported"
    added = set(json.loads(config.SETTINGS_PATH.read_text("utf-8"))) - before
    assert added <= {"pb_import_asked", "credential_set_at"}, added      # the cookies go to the vault
    stored = set(config.get_settings())
    known = {k for fields in config.PLATFORM_CREDENTIAL_FIELDS.values() for k in fields}
    assert {k for k in stored if k.startswith("fa_cookie")} <= known      # only the fixed FA keys
    assert "fake-a-fa-friend" not in config.SETTINGS_PATH.read_text("utf-8")


def test_routes_stay_signed_in_only_and_out_of_backups():
    import dashboard
    from routes import backup_api
    paths = ["/api/pb-import/uploads", "/api/pb-import/uploads/x/thumb/s1", "/api/pb-import/thumb/s1"]
    for p in paths:
        assert p not in dashboard._AUTH_EXEMPT_PATHS
        assert not any(p.startswith(pre) for pre in dashboard._AUTH_EXEMPT_PREFIXES)
    assert "pb_upload" not in backup_api._BACKUP_DIRS


def test_upload_logs_carry_counts_only(env, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    c, uid = _ready(env)
    c.get(f"/api/pb-import/uploads/{uid}/submissions")
    c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"ids": ["s1"]})
    _wait()
    text = " | ".join(r.getMessage() for r in caplog.records if r.name in ("pb_import", "pb_upload"))
    for secret in ("Sample Fox", "Penwright", "SecondHandle", "fake-a-", "fox.png", ".zip", "SomeoneElse"):
        assert secret not in text, secret


def test_the_test_suite_can_never_reach_the_real_archive(tmp_path):
    from posting import artwork_reader
    config.save_settings({"artwork_archive_path": str(tmp_path / "not-made")})
    assert "m_x" not in str(artwork_reader.get_artwork_archive_path())
