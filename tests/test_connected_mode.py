"""Connected mode (SYNCTRUTH phases 1–2, 4.13.0).

The desktop as a window onto its server: the mode itself, the agent's queue and delivery,
the two desktop-only jobs (browser login, file picking) handed to the server, the server's
receiving routes, and the bridge shim. No pywebview anywhere in here.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import desktop_agent as da
from routes import media_api, settings_api


# ── the mode ─────────────────────────────────────────────────────────────────

def test_connected_is_a_valid_mode_and_the_server_polls():
    assert config.SETUP_MODE_CONNECTED in config.VALID_SETUP_MODES


def test_polling_owner_for_connected(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: {"setup_mode": config.SETUP_MODE_CONNECTED})
    assert config.get_polling_owner("desktop") == "server"
    monkeypatch.setattr(config, "get_settings", lambda: {"setup_mode": config.SETUP_MODE_STANDALONE})
    assert config.get_polling_owner("desktop") == "local"


def test_decide_mode_and_target():
    saved = {"setup_mode": "connected", "posting_server_url": "https://box.ts.net/", "posting_server_api_key": "pp_k"}
    assert da.decide_mode([], saved) == "connected"
    assert da.decide_mode(["--standalone"], saved) == "standalone"
    assert da.decide_mode(["--connect", "http://127.0.0.1:8499"], {}) == "connected"
    assert da.decide_mode([], {"setup_mode": "standalone"}) == "standalone"
    assert da.connect_target([], saved) == ("https://box.ts.net", "pp_k")
    assert da.connect_target(["--connect", "http://127.0.0.1:8499/"], saved) == ("http://127.0.0.1:8499", "pp_k")
    assert da.connect_target(["--connect"], saved) == ("https://box.ts.net", "pp_k")


# ── the queue ────────────────────────────────────────────────────────────────

def test_queue_persists_dedups_caps_and_backs_off(tmp_path, monkeypatch):
    q = da.Queue(tmp_path / "q.json")
    a = q.add("cookies", {"platform": "fa", "cookies": {"a": "1"}}, dedup_key="cookies:fa:None")
    q.add("cookies", {"platform": "fa", "cookies": {"a": "2"}}, dedup_key="cookies:fa:None")
    assert q.pending_count() == 1 and q.pending()[0]["payload"]["cookies"]["a"] == "2"
    assert q.pending()[0]["id"] != a
    q2 = da.Queue(tmp_path / "q.json")                       # survives a restart
    assert q2.pending_count() == 1
    monkeypatch.setattr(da, "MAX_QUEUE", 5)
    for i in range(10):
        q2.add("upload", {"path": f"/x/{i}"})
    assert q2.pending_count() == 5
    item = q2.pending()[0]
    q2.failed(item["id"], "ConnectError", now=1000.0)
    assert q2.due(now=1000.0) == [i for i in q2.due(now=1000.0) if i["id"] != item["id"]]
    assert [i for i in q2.pending() if i["id"] == item["id"]][0]["next_at"] == 1030.0
    q2.failed(item["id"], "ConnectError", now=1100.0)
    assert [i for i in q2.pending() if i["id"] == item["id"]][0]["next_at"] == 1160.0
    q2.remove(item["id"])
    assert q2.pending_count() == 4


# ── a fake server for the agent ──────────────────────────────────────────────

class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, up=True, known=()):
        self.up, self.known = up, set(known)
        self.cookies, self.uploads = [], []

    def get(self, url, **kw):
        if not self.up:
            raise ConnectionError("down")
        if url.endswith("/api/health"):
            return FakeResp(200, {"status": "ok"})
        if "/api/media/exists/" in url:
            sha = url.rsplit("/", 1)[1]
            return FakeResp(200, {"exists": sha in self.known, "path": f"/srv/inbox/{sha[:16]}_x" if sha in self.known else None})
        return FakeResp(404)

    def post(self, url, json=None, data=None, files=None, **kw):
        if not self.up:
            raise ConnectionError("down")
        if url.endswith("/browser-login/result"):
            self.cookies.append(json)
            return FakeResp(200, {"ok": True})
        if url.endswith("/api/media/upload"):
            name, fh, _ = files["file"]
            body = fh.read()
            sha = hashlib.sha256(body).hexdigest()
            self.known.add(sha)
            self.uploads.append((name, data.get("kind"), sha))
            return FakeResp(200, {"path": f"/srv/inbox/{sha[:16]}_{name}", "sha256": sha, "existing": False})
        return FakeResp(404)

    def close(self):
        pass


def _agent(tmp_path, http, login_fn=None):
    return da.Agent("https://box.example", "pp_key", http=http, queue=da.Queue(tmp_path / "q.json"), login_fn=login_fn)


def test_login_sends_directly_when_the_server_is_up(tmp_path):
    http = FakeHttp(up=True)
    ag = _agent(tmp_path, http, login_fn=lambda plat, extra, acc: {"fa_cookie_a": "A", "fa_cookie_b": "B"})
    out = ag.login("fa", {"fa_username": "Inkwolf"}, 7)
    assert out["ok"] is True and "queued" not in out
    assert http.cookies == [{"platform": "fa", "cookies": {"fa_cookie_a": "A", "fa_cookie_b": "B"}, "account_id": 7,
                             "extra_fields": {"fa_username": "Inkwolf"}}]
    assert ag.queue.pending_count() == 0


def test_login_queues_when_the_server_is_down_and_drains_later(tmp_path):
    http = FakeHttp(up=False)
    ag = _agent(tmp_path, http, login_fn=lambda plat, extra, acc: {"da_cookie": "X"})
    out = ag.login("da", {}, None)
    assert out["ok"] is True and out["queued"] is True and ag.queue.pending_count() == 1
    assert ag.drain(now=0.0) == 0 and ag.queue.pending_count() == 1     # still down: backoff, not dropped
    http.up = True
    assert ag.drain(now=10_000.0) == 1 and ag.queue.pending_count() == 0
    assert http.cookies[0]["platform"] == "da"


def test_drain_drops_permanent_refusals_but_keeps_auth_and_outages(tmp_path):
    class Refusing(FakeHttp):
        def __init__(self, status):
            super().__init__(up=True)
            self.status = status

        def post(self, url, json=None, data=None, files=None, **kw):
            return FakeResp(self.status, text="Account 5 is not a da account" if self.status == 400 else "nope")

    for status, kept in ((400, False), (422, False), (401, True), (429, True), (503, True)):
        q = da.Queue(tmp_path / f"q{status}.json")
        q.add("cookies", {"platform": "da", "cookies": {"x": "y"}, "account_id": 5})
        ag = da.Agent("https://box.example", "pp_key", http=Refusing(status), queue=q)
        assert ag.drain(now=0.0) == 0
        assert (q.pending_count() == 1) is kept, status
    assert da.permanent_failure("HTTP 400: bad") and not da.permanent_failure("HTTP 401: key") and not da.permanent_failure("ConnectError")


def test_login_cancelled_or_failed(tmp_path):
    ag = _agent(tmp_path, FakeHttp(), login_fn=lambda *a: None)
    assert ag.login("fa")["ok"] is False
    ag2 = _agent(tmp_path, FakeHttp(), login_fn=lambda *a: (_ for _ in ()).throw(RuntimeError("no screen")))
    assert "no screen" in ag2.login("fa")["message"]


def test_upload_direct_then_skip_when_known(tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG fake")
    http = FakeHttp(up=True)
    ag = _agent(tmp_path, http)
    out = ag.upload(str(f))
    assert out["ok"] and out["path"].startswith("/srv/inbox/") and out["path"].endswith("_pic.png")
    assert len(http.uploads) == 1
    out2 = ag.upload(str(f))
    assert out2["ok"] and len(http.uploads) == 1                        # exists → no second upload
    assert ag.upload(str(tmp_path / "missing.png"))["ok"] is False


def test_upload_queues_when_down(tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"data")
    http = FakeHttp(up=False)
    ag = _agent(tmp_path, http)
    out = ag.upload(str(f))
    assert out["ok"] is False and out["queued"] is True and ag.queue.pending_count() == 1
    http.up = True
    assert ag.drain(now=10_000.0) == 1 and http.uploads[0][0] == "pic.png"


def test_agent_api_picker_returns_the_server_path(tmp_path):
    f = tmp_path / "art.jpg"
    f.write_bytes(b"jpg")
    http = FakeHttp(up=True)
    ag = _agent(tmp_path, http)
    api = da.AgentApi(ag, picker=lambda: [str(f)])
    got = api.open_image_dialog()
    assert got == [f"/srv/inbox/{hashlib.sha256(b'jpg').hexdigest()[:16]}_art.jpg"]
    assert da.AgentApi(ag, picker=lambda: []).open_image_dialog() == []
    st = api.agent_status()
    assert st["server_url"] == "https://box.example" and st["pending"] == 0
    assert api.agent_login("fa", {}, "")["ok"] is False                 # no login_fn result → cancelled


def test_offline_page_names_the_server(tmp_path):
    ag = _agent(tmp_path, FakeHttp(up=False))
    html = da.offline_page("https://box.ts.net", ag)
    assert "box.ts.net" in html and "0 items" in html


# ── the server's receiving routes ────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(media_api, "INBOX", tmp_path / "inbox")
    app = FastAPI()
    app.include_router(media_api.media_router)
    app.include_router(settings_api.settings_router)
    return TestClient(app)


def test_media_upload_and_exists(api, tmp_path):
    body = b"hello art"
    sha = hashlib.sha256(body).hexdigest()
    assert api.get(f"/api/media/exists/{sha}").json() == {"exists": False, "path": None}
    r = api.post("/api/media/upload", data={"kind": "inbox", "sha256": sha}, files={"file": ("my art (v2).png", body)})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sha256"] == sha and out["existing"] is False and out["size"] == len(body)
    p = Path(out["path"])
    assert p.is_file() and p.read_bytes() == body and p.name == f"{sha[:16]}_my art (v2).png"
    assert api.get(f"/api/media/exists/{sha}").json() == {"exists": True, "path": str(p)}
    r2 = api.post("/api/media/upload", data={"kind": "artwork"}, files={"file": ("again.png", body)})
    assert r2.json()["existing"] is True and r2.json()["path"] == str(p)
    assert api.post("/api/media/upload", data={"sha256": "0" * 64}, files={"file": ("x.png", body)}).status_code == 400
    assert api.post("/api/media/upload", data={"kind": "weird"}, files={"file": ("x.png", body)}).status_code == 400
    assert api.get("/api/media/exists/nothex").status_code == 400
    assert not list((tmp_path / "inbox").glob(".upload-*"))            # no temp files left behind


def test_media_upload_size_cap(api, monkeypatch):
    monkeypatch.setattr(media_api, "MEDIA_UPLOAD_MAX_BYTES", 10)
    r = api.post("/api/media/upload", files={"file": ("big.png", b"x" * 11)})
    assert r.status_code == 413


def test_browser_login_result_saves_like_the_local_popup(api, monkeypatch):
    from auth import browser_login as bl
    captured = {}

    def fake_save(platform, creds, account_id):
        captured.update(platform=platform, creds=creds, account_id=account_id)
        return {k: True for k in creds}

    monkeypatch.setattr(bl, "_save_browser_creds", fake_save)
    plat = sorted(bl.PLATFORM_LOGIN)[0]
    r = api.post("/api/settings/browser-login/result",
                 json={"platform": plat, "cookies": {"c1": "v1"}, "account_id": 3, "extra_fields": {"name": "Penwright", "n": 5}})
    assert r.status_code == 200, r.text
    assert captured == {"platform": plat, "creds": {"c1": "v1", "name": "Penwright"}, "account_id": 3}
    assert r.json()["saved"] == ["c1", "name"]
    assert api.post("/api/settings/browser-login/result", json={"platform": "nope", "cookies": {"a": "b"}}).status_code == 400
    assert api.post("/api/settings/browser-login/result", json={"platform": plat, "cookies": {}}).status_code == 400


def test_setup_mode_connected(api, monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    monkeypatch.setattr(config, "get_settings", lambda: dict(saved))
    import auto_sync
    monkeypatch.setattr(auto_sync, "pull_once", lambda: (_ for _ in ()).throw(AssertionError("must not sync")))
    r = api.post("/api/settings/setup-mode", json={"mode": "connected"})
    assert r.status_code == 400
    r = api.post("/api/settings/setup-mode", json={"mode": "connected", "posting_server_url": "http://192.168.1.5:8420",
                                                   "posting_server_api_key": "pp_x"})
    assert r.status_code == 400 and "Tailscale" in r.json()["detail"]
    r = api.post("/api/settings/setup-mode", json={"mode": "connected", "posting_server_url": "http://box.tail1.ts.net:8420/",
                                                   "posting_server_api_key": "pp_x"})
    assert r.status_code == 200, r.text
    assert saved["setup_mode"] == "connected" and saved["posting_server_url"] == "http://box.tail1.ts.net:8420"
    assert saved["auto_sync_enabled"] is False


# ── migration (phase 3, 4.14.0) ──────────────────────────────────────────────

def test_retire_local_database(tmp_path):
    import datetime
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"pawpoller.db{suffix}").write_bytes(b"x")
    moved = da.retire_local_database(tmp_path, now=datetime.datetime(2026, 9, 5, 12, 0, 0))
    assert moved == ["pawpoller.db.retired-20260905-120000", "pawpoller.db.retired-20260905-120000-wal",
                     "pawpoller.db.retired-20260905-120000-shm"]
    assert not (tmp_path / "pawpoller.db").exists() and (tmp_path / moved[0]).read_bytes() == b"x"
    assert da.retire_local_database(tmp_path) == []                   # nothing left to retire


def test_connect_migrate_pushes_then_flips(api, monkeypatch):
    from routes import mirror_api
    saved = {"setup_mode": "paired_desktop", "posting_server_url": "http://box.tail1.ts.net:8420", "posting_server_api_key": "pp_x"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(saved))
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    from posting import scheduler
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "desktop")
    pushes = []

    async def fake_push(url, key, *, confirm_deletes=(), dry_run=False):
        pushes.append((url, key, dry_run))
        return {"pushed": 3}

    monkeypatch.setattr(mirror_api, "_run_shr_push", fake_push)
    r = api.post("/api/settings/connect-migrate", json={})
    assert r.status_code == 200, r.text
    assert pushes == [("http://box.tail1.ts.net:8420", "pp_x", False)]
    assert saved["setup_mode"] == "connected" and saved["auto_sync_enabled"] is False
    assert "reopen" in r.json()["next"].lower()
    # a server has nothing to migrate; a standalone has no server
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "server")
    assert api.post("/api/settings/connect-migrate", json={}).status_code == 409
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "desktop")
    saved.update(posting_server_url="")
    assert api.post("/api/settings/connect-migrate", json={}).status_code == 400


def test_seed_from_runs_once_on_a_server_only(monkeypatch):
    from routes import mirror_api
    from posting import scheduler
    app = FastAPI()
    app.include_router(mirror_api.mirror_router)
    client = TestClient(app)
    saved = {}
    monkeypatch.setattr(config, "get_settings", lambda: dict(saved))
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    started = []

    async def fake_pull(url, key, **kw):
        started.append((url, key, kw.get("push_first")))
        return {"ok": True}

    monkeypatch.setattr(mirror_api, "_run_pull", fake_pull)
    monkeypatch.setattr(mirror_api, "_seed_would_replace", lambda: {t: 0 for t in mirror_api._SEED_TABLES})
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "desktop")
    assert client.post("/api/mirror/seed-from", json={"server_url": "http://laptop.tail1.ts.net:8420", "api_key": "pp_d"}).status_code == 409
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "server")
    assert client.post("/api/mirror/seed-from", json={"server_url": "http://laptop.tail1.ts.net:8420"}).status_code == 400
    assert client.post("/api/mirror/seed-from", json={"server_url": "http://192.168.1.9:8420", "api_key": "pp_d"}).status_code == 400
    r = client.post("/api/mirror/seed-from", json={"server_url": "http://laptop.tail1.ts.net:8420", "api_key": "pp_d"})
    assert r.status_code == 200, r.text
    assert saved.get("mirror_seeded_at")
    assert client.post("/api/mirror/seed-from", json={"server_url": "http://laptop.tail1.ts.net:8420", "api_key": "pp_d"}).status_code == 409
    assert started and started[0][2] is False                        # a seed never pushes first


def test_seed_from_refuses_a_server_that_already_holds_data(monkeypatch):
    """4.14.1: 'fresh server' is checked. A populated server only seeds with confirm: "replace"."""
    from routes import mirror_api
    from posting import scheduler
    app = FastAPI()
    app.include_router(mirror_api.mirror_router)
    client = TestClient(app)
    saved = {}
    monkeypatch.setattr(config, "get_settings", lambda: dict(saved))
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    monkeypatch.setattr(scheduler, "detect_runtime_mode", lambda: "server")
    started = []

    async def fake_pull(url, key, **kw):
        started.append(url)
        return {"ok": True}

    monkeypatch.setattr(mirror_api, "_run_pull", fake_pull)
    monkeypatch.setattr(mirror_api, "_seed_would_replace",
                        lambda: {"accounts": 3, "submissions": 120, "masterpieces": 0, "posts": 0})
    body = {"server_url": "http://laptop.tail1.ts.net:8420", "api_key": "pp_d"}
    r = client.post("/api/mirror/seed-from", json=body)
    assert r.status_code == 409
    assert "3 accounts, 120 submissions" in r.json()["detail"] and "replace" in r.json()["detail"]
    assert not saved.get("mirror_seeded_at") and not started          # nothing stamped, nothing started
    r = client.post("/api/mirror/seed-from", json={**body, "confirm": "replace"})
    assert r.status_code == 200, r.text
    assert saved.get("mirror_seeded_at") and started


def test_seed_would_replace_counts_the_tables_people_notice(tmp_path, monkeypatch):
    import sqlite3
    from routes import mirror_api
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE accounts (id INTEGER)"); db.execute("INSERT INTO accounts VALUES (1), (2)")
    db.execute("CREATE TABLE submissions (id INTEGER)")
    # masterpieces / posts missing on purpose: an old schema counts as empty, never as an error
    monkeypatch.setattr("database.db.get_connection", lambda: db)
    assert mirror_api._seed_would_replace() == {"accounts": 2, "submissions": 0, "masterpieces": 0, "posts": 0}


# ── the bridge shim and the wizard ───────────────────────────────────────────

def test_bridge_shim_and_wizard_wiring():
    repo = Path(__file__).resolve().parents[1]
    shim = (repo / "frontend" / "js" / "desktop_bridge.js").read_text(encoding="utf-8")
    assert "agent_login" in shim and "pywebviewready" in shim and "API.browserLogin" in shim
    index = (repo / "frontend" / "index.html").read_text(encoding="utf-8")
    assert index.index("/js/api.js") < index.index("/js/desktop_bridge.js") < index.index("/js/app.js")
    app_js = (repo / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'data-mode="connected"' in app_js
    assert "selectedMode === 'connected' ? 'connected' : 'paired_desktop'" in app_js
    main_py = (repo / "main.py").read_text(encoding="utf-8")
    assert "def run_connected(" in main_py and "def run_standalone(" in main_py
    assert "init_db()" not in main_py.split("def run_connected(")[1].split("def run_standalone(")[0]
