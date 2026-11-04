"""Change server (spec 072, CHANGESERVER).

A connected desktop re-pointed at a different PawPoller from a small local window: the
address checked without the key, the host confirmed, the key checked, both saved in one
vault write, and a fresh agent takes over. Also the bridge change that came with it: no
connected-mode window gets a pywebview ``js_api`` object, because pywebview resolves any
dotted attribute path a page sends on one (private names included).

No pywebview window opens here. ``main.run_connected`` runs against a stand-in ``webview``
module, with the database retirement, the agent's thread and the tray stubbed, so the real
data folder is never touched.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import types

import httpx
import pytest

import config
import desktop_agent as da

OLD = "https://old.example"
NEW = "https://new.example"
OLD_KEY = "pp_oldkey_1234"
NEW_KEY = "pp_newkey_5678"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setattr(da, "_vault_cipher", lambda: Fernet(key))
    monkeypatch.setattr(da, "QUEUE_PATH", tmp_path / "agent_queue.json")
    monkeypatch.setattr(da, "HELPER_QUEUE_PATH", tmp_path / "helper_queue.json")
    monkeypatch.setattr(da.Agent, "start", lambda self: None)       # no delivery thread in a test


# ── a stand-in server ────────────────────────────────────────────────────────

class Srv:
    """What answers at an address: a PawPoller by default."""

    def __init__(self):
        self.health = (200, {"status": "ok", "version": config.APP_VERSION})
        self.auth_required = True
        self.keys = {NEW_KEY}
        self.bad_key_status = 401
        self.requests: list[httpx.Request] = []
        self.raw = None                      # (status, bytes, headers) to replace the health reply
        self.stream = None                   # an httpx.SyncByteStream for the health reply

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/health":
            if self.stream is not None:
                return httpx.Response(200, stream=self.stream)
            if self.raw is not None:
                status, body, headers = self.raw
                return httpx.Response(status, content=body, headers=headers)
            status, body = self.health
            return httpx.Response(status, json=body)
        if path == "/api/auth/dashboard-status":
            return httpx.Response(200, json={"auth_required": self.auth_required})
        if path == "/api/settings/sync/status":
            auth = request.headers.get("authorization", "")
            if any(auth == f"Bearer {k}" for k in self.keys):
                return httpx.Response(200, json={"version": config.APP_VERSION})
            return httpx.Response(self.bad_key_status, json={"detail": "<script>nope</script> " + auth})
        return httpx.Response(404)


@pytest.fixture
def srv(monkeypatch):
    s = Srv()
    monkeypatch.setattr(da, "helper_client", lambda: httpx.Client(
        transport=httpx.MockTransport(s.handler), follow_redirects=False))
    return s


class FakeHttp:
    def __init__(self):
        self.cookies = []

    def get(self, url, **kw):
        return types.SimpleNamespace(status_code=200, json=lambda: {"status": "ok"})

    def post(self, url, json=None, **kw):
        self.cookies.append((url, json))
        return types.SimpleNamespace(status_code=200, text="", json=lambda: {"ok": True})

    def close(self):
        pass


class Rig:
    """A Connection with stand-in window callables, recording everything it does."""

    def __init__(self, tmp_path, agent=True):
        self.queue = da.Queue(tmp_path / "agent_queue.json")
        self.old = da.Agent(OLD, OLD_KEY, http=FakeHttp(), queue=self.queue) if agent else None
        self.nav: list[tuple] = []
        self.saves: list[dict] = []
        self.made: list[da.Agent] = []
        self.forms = 0
        self.closed = 0

        def make(origin, key, q):
            a = da.Agent(origin, key, http=FakeHttp(), queue=q)
            self.made.append(a)
            return a

        self.conn = da.Connection(
            self.old, queue=self.queue, current_url=OLD, saved_key=OLD_KEY,
            load_url=lambda u: self.nav.append(("url", u)),
            load_html=lambda h: self.nav.append(("html", h)),
            open_form=self._open, close_form=self._close,
            save=lambda d: self.saves.append(dict(d)), make_agent=make)

    def _open(self):
        self.forms += 1

    def _close(self):
        self.closed += 1

    def form(self) -> str:
        self.conn.form_html()
        return self.conn.form_nonce

    def check(self, address=NEW):
        return self.conn.cs_check(self.form(), address)

    def go(self, address=NEW, key=NEW_KEY, send=True):
        n = self.form()
        r = self.conn.cs_check(n, address)
        assert r["ok"], r
        return self.conn.cs_connect(n, key, send)


@pytest.fixture
def rig(tmp_path, srv):
    return Rig(tmp_path)


# ── T002/T003: the key stays with its address ───────────────────────────────

def test_connect_override_gets_no_key():
    saved = {"posting_server_url": "https://box.example/", "posting_server_api_key": "pp_k"}
    assert da.connect_target(["--connect", "https://other.example"], saved) == ("https://other.example", "")
    assert da.connect_target(["--connect", "https://box.example"], saved) == ("https://box.example", "pp_k")
    assert da.connect_target(["--connect", "https://box.example/"], saved) == ("https://box.example", "pp_k")
    assert da.connect_target([], saved) == ("https://box.example", "pp_k")


def test_url_and_key_are_both_vault_fields():
    """One save_settings call swaps the pair atomically only while both live in the vault (R3)."""
    assert config.is_credential_key("posting_server_url")
    assert config.is_credential_key("posting_server_api_key")


# ── T004: queue tags ─────────────────────────────────────────────────────────

def test_queue_file_holds_no_server_address(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    q.add("upload", {"path": "x"}, server=NEW)
    raw = (tmp_path / "q.json").read_text(encoding="utf-8")
    assert "new.example" not in raw and da.server_tag(NEW) in raw


def test_legacy_untagged_items_still_drain(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    q.add("cookies", {"platform": "fa", "cookies": {}, "account_id": None})      # no server: an older version's
    http = FakeHttp()
    assert da.Agent(OLD, OLD_KEY, http=http, queue=q).drain() == 1 and q.pending_count() == 0


def test_drain_skips_other_servers_items(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    q.add("cookies", {"platform": "fa", "cookies": {}, "account_id": None}, server=OLD)
    http = FakeHttp()
    assert da.Agent(NEW, NEW_KEY, http=http, queue=q).drain() == 0 and http.cookies == []


def test_drain_stops_between_items(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    for n in range(3):
        q.add("cookies", {"platform": "fa", "cookies": {}, "account_id": n}, dedup_key=str(n), server=OLD)
    ag = da.Agent(OLD, OLD_KEY, queue=q)
    sent = []

    def send(payload):
        sent.append(payload)
        ag.stop()
        return True, ""
    ag.send_cookies = send
    ag.drain()
    assert len(sent) == 1 and q.pending_count() == 2


def test_stopped_agent_does_not_queue(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    ag = da.Agent(OLD, OLD_KEY, queue=q, login_fn=lambda p, e, a: {"c": "1"})
    ag.stop()
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    assert ag.login("fa")["message"] == da.MSG_CS_SWITCHED
    assert ag.upload(str(f))["message"] == da.MSG_CS_SWITCHED
    assert q.pending_count() == 0


def test_old_agent_leaves_retagged_items_alone(tmp_path):
    q = da.Queue(tmp_path / "q.json")
    iid = q.add("upload", {"path": "x"}, server=OLD)
    q.retag([iid], NEW)
    q.remove(iid, server=OLD)
    q.failed(iid, "HTTP 500", server=OLD)
    item = q.pending()[0]
    assert item["server"] == da.server_tag(NEW) and item["attempts"] == 0


def test_item_queued_after_check_is_not_sent_to_the_new_server(rig):
    n = rig.form()
    assert rig.conn.cs_check(n, NEW)["ok"]
    rig.queue.add("cookies", {"platform": "fa", "cookies": {"a": "b"}, "account_id": 3}, server=OLD)   # late
    assert rig.conn.cs_connect(n, NEW_KEY, True)["ok"]
    assert rig.queue.pending_count() == 0


# ── T005: address and key clean-up ──────────────────────────────────────────

@pytest.mark.parametrize("address,msg", [
    ("https://user:pw@new.example", da.MSG_CS_USERINFO),
    ("https://new.example@evil.example", da.MSG_CS_USERINFO),
    ("https://good.example\\@evil.example", da.MSG_CS_BAD_ADDRESS),
    ("https://new .example", da.MSG_CS_BAD_ADDRESS),
    ("https://new.example\x07", da.MSG_CS_BAD_ADDRESS),
    ("https://new.ex​ample", da.MSG_CS_BAD_ADDRESS),
    ("new.example", da.MSG_CS_BAD_ADDRESS),
    ("ftp://new.example", da.MSG_CS_BAD_ADDRESS),
    ("https://new.example:99999", da.MSG_CS_BAD_ADDRESS),
    ("", da.MSG_CS_EMPTY_ADDRESS),
    (None, da.MSG_CS_EMPTY_ADDRESS),
    ({"x": 1}, da.MSG_CS_EMPTY_ADDRESS),
    ("http://203.0.113.5:8420", da.MSG_CS_UNSAFE),
])
def test_bad_address_refused(rig, srv, address, msg):
    r = rig.check(address)
    assert r == {"ok": False, "message": msg}
    assert srv.requests == []


def test_confirm_host_is_the_host_contacted(rig, srv):
    r = rig.check("https://faß.example/")
    assert r["ok"] and r["to_host"] == "xn--fa-hia.example"
    assert {req.url.raw_host.decode() for req in srv.requests} == {"xn--fa-hia.example"}


def test_ipv6_origin_keeps_brackets():
    assert da.clean_origin("http://[::1]:8420/x") == ("http://[::1]:8420", None)


def test_dashboard_link_reduced_to_origin():
    assert da.clean_origin("  https://New.Example:8443/#/settings?tab=x  ") == ("https://new.example:8443", None)


def test_bad_key_refused(rig, srv):
    n = rig.form()
    assert rig.conn.cs_check(n, NEW)["ok"]
    before = len(srv.requests)
    for key, msg in (("", da.MSG_CS_EMPTY_KEY), ("short", da.MSG_CS_BAD_KEY), ("x" * 257, da.MSG_CS_BAD_KEY),
                     ("pp_new\nkey_5678", da.MSG_CS_BAD_KEY), ("pp new key 5678", da.MSG_CS_BAD_KEY)):
        assert rig.conn.cs_connect(n, key, True) == {"ok": False, "message": msg}
    assert len(srv.requests) == before and rig.saves == []


# ── T006: the checks ─────────────────────────────────────────────────────────

def test_keyless_step_sends_no_key(rig, srv):
    assert rig.check()["ok"]
    assert [r.url.path for r in srv.requests] == ["/api/health", "/api/auth/dashboard-status"]
    assert all("authorization" not in r.headers for r in srv.requests)


def test_identity_encoding_requested(rig, srv):
    rig.check()
    assert all(r.headers.get("accept-encoding") == "identity" for r in srv.requests)


def test_relay_refused(rig, srv):
    srv.health = (200, {"status": "ok", "mode": "relay", "version": "4.88.0"})
    assert rig.check() == {"ok": False, "message": da.MSG_CS_RELAY}


@pytest.mark.parametrize("raw", [
    (200, b"<html>hello</html>", {}),
    (200, json.dumps({"status": "ok"}).encode(), {}),
    (200, json.dumps(["ok"]).encode(), {}),
    (500, json.dumps({"status": "ok", "version": "1"}).encode(), {}),
    (302, b"", {"location": "https://evil.example/api/health"}),
    (200, b"x" * (da.CS_BODY_CAP + 10), {}),
])
def test_not_pawpoller_refused(rig, srv, raw):
    srv.raw = raw
    assert rig.check() == {"ok": False, "message": da.MSG_CS_NOT_PAWPOLLER}
    assert all(r.url.host == "new.example" for r in srv.requests)        # a redirect isn't followed


def test_compressed_reply_refused(rig, srv):
    import gzip
    body = json.dumps({"status": "ok", "version": config.APP_VERSION}).encode()
    srv.raw = (200, gzip.compress(body), {"content-encoding": "gzip"})
    assert rig.check() == {"ok": False, "message": da.MSG_CS_NOT_PAWPOLLER}


def test_trickling_server_gives_up_in_time(rig, srv, monkeypatch):
    import time as _t

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                _t.sleep(0.01)
                yield b" "
    monkeypatch.setattr(da, "CS_DEADLINE", 0.1)
    srv.stream = Trickle()
    t0 = _t.monotonic()
    assert rig.check() == {"ok": False, "message": da.MSG_CS_TIMEOUT}
    assert _t.monotonic() - t0 < 2


def test_unreachable_names_tailscale(rig, monkeypatch):
    def boom():
        def handler(request):
            raise httpx.ConnectError("no route <script>")
        return httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(da, "helper_client", boom)
    assert rig.check() == {"ok": False, "message": da.MSG_CS_UNREACHABLE}
    assert rig.check("http://box.tail1234.ts.net:8420")["message"] == da.MSG_CS_UNREACHABLE + da.MSG_CS_TAILSCALE


def test_open_instance_is_flagged(rig, srv):
    srv.auth_required = False
    r = rig.check()
    assert r["ok"] and r["open_instance"] is True
    out = rig.conn.cs_connect(rig.conn.form_nonce, NEW_KEY, True)
    assert out["ok"] and out["open_instance"] is True


@pytest.mark.parametrize("status,msg", [(401, da.MSG_CS_KEY_REFUSED), (403, da.MSG_CS_KEY_REFUSED),
                                        (429, da.MSG_CS_TOO_MANY), (500, da.MSG_CS_KEY_ODD)])
def test_key_check_answers(rig, srv, status, msg):
    srv.bad_key_status = status
    assert rig.go(key="pp_wrong_key_0") == {"ok": False, "message": msg}
    assert rig.saves == []


def test_refusal_messages_are_fixed(rig, srv):
    srv.bad_key_status = 401
    out = rig.go(key="pp_wrong_key_0")
    allowed = {v for k, v in vars(da).items() if k.startswith("MSG_CS_")}
    assert out["message"] in allowed and "<script>" not in out["message"] and "pp_wrong" not in out["message"]


def test_version_note_is_cleaned(rig, srv):
    srv.health = (200, {"status": "ok", "version": "9.9.9<img src=x>" + "y" * 50})
    r = rig.check()
    assert r["ok"] and "<" not in r["version_note"] and "9.9.9img src=xyyyyyy" in r["version_note"]


# ── T012/T013: the bridge ────────────────────────────────────────────────────

def test_main_window_exposes_exactly(rig):
    assert [f.__name__ for f in rig.conn.main_functions()] == [
        "open_image_dialog", "agent_login", "agent_status", "fa_journal", "open_change_server"]
    assert [f.__name__ for f in rig.conn.form_functions()] == ["cs_check", "cs_connect", "cs_cancel"]


class _Inline:
    def __init__(self, target=None, args=(), **kw):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def _bridge(monkeypatch, conn, js_api):
    """Drive pywebview's real js_bridge_call against a stand-in window; return what pages got back."""
    wu = pytest.importorskip("webview.util")      # desktop-only; the server image has no pywebview
    monkeypatch.setattr(wu, "Thread", _Inline)
    replies: list[str] = []
    win = types.SimpleNamespace(_functions={f.__name__: f for f in conn.main_functions() + conn.form_functions()},
                                _js_api=js_api, evaluate_js=replies.append, _callbacks={})
    probes = [("agent_status.__self__.agent.api_key", []),
              ("open_change_server.__self__.agent.__dict__.get", ["api_key"]),
              ("agent.__dict__.get", ["api_key"]),
              ("__dict__.get", ["offline_nonce"]),
              ("__dict__.get", ["_saved_key"]),
              ("cs_check.__globals__.get", ["config"]),
              ("__setattr__", ["offline_nonce", "x"]),
              ("_switch", [NEW, "pp_evil_key_1", True])]
    for i, (name, params) in enumerate(probes):
        wu.js_bridge_call(win, name, params, f"v{i}")
    return "\n".join(replies)


def test_bridge_refuses_private_and_dotted_names(rig, monkeypatch, caplog):
    conn = rig.conn
    conn.offline_html()
    nonce = conn.offline_nonce
    got = _bridge(monkeypatch, conn, None)
    assert OLD_KEY not in got and nonce not in got
    assert conn.agent is rig.old and conn.current_url == OLD and conn.offline_nonce == nonce
    assert rig.saves == [] and rig.made == []


def test_bridge_canary_a_js_api_object_leaks(rig, monkeypatch):
    """The reason for window.expose: the same probes against js_api=conn reach the key."""
    got = _bridge(monkeypatch, rig.conn, rig.conn)
    assert OLD_KEY in got


# ── T014: nonces ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "x", {"a": 1}, 12, "ñonce", ["n"]])
def test_open_change_server_needs_the_offline_nonce(rig, bad):
    rig.conn.offline_html()
    assert rig.conn.open_change_server(bad) is False and rig.forms == 0
    assert rig.conn.open_change_server(rig.conn.offline_nonce) is True and rig.forms == 1
    n = rig.conn.offline_nonce
    rig.conn.show_server()
    assert rig.conn.open_change_server(n) is False and rig.forms == 1


@pytest.mark.parametrize("bad", [None, "", "x", {"a": 1}, 12, "ñonce"])
def test_form_bridge_refuses_a_wrong_nonce(rig, srv, bad):
    rig.form()
    stale = {"ok": False, "message": da.MSG_CS_STALE}
    assert rig.conn.cs_check(bad, NEW) == stale
    assert rig.conn.cs_connect(bad, NEW_KEY, True) == stale
    assert rig.conn.cs_cancel(bad) == stale
    assert srv.requests == [] and rig.closed == 0


def test_stale_beats_busy(rig):
    n = rig.form()
    rig.conn._busy.acquire()
    try:
        assert rig.conn.cs_check("wrong", NEW)["message"] == da.MSG_CS_STALE
        assert rig.conn.cs_check(n, NEW)["message"] == da.MSG_CS_BUSY
    finally:
        rig.conn._busy.release()


def test_fresh_form_nonce_each_open(rig, srv):
    first = rig.form()
    rig.conn.cs_cancel(first)
    rig.conn.form_closed()
    second = rig.form()
    assert first != second and rig.conn._cancel is False
    assert rig.conn.cs_check(first, NEW)["message"] == da.MSG_CS_STALE
    assert rig.conn.cs_check(second, NEW)["ok"]


# ── T015: the switch ─────────────────────────────────────────────────────────

def test_connect_before_check_refused(rig, srv):
    n = rig.form()
    assert rig.conn.cs_connect(n, NEW_KEY, True)["message"] == da.MSG_CS_NOT_CHECKED
    assert rig.conn.cs_check(n, NEW)["ok"]
    srv.health = (200, {"status": "ok", "mode": "relay", "version": "1"})
    assert not rig.conn.cs_check(n, "https://other.example")["ok"]
    assert rig.conn.cs_connect(n, NEW_KEY, True)["message"] == da.MSG_CS_NOT_CHECKED
    assert rig.saves == []


def test_same_key_new_host_refused(rig, srv):
    srv.keys.add(OLD_KEY)
    assert rig.go(key=OLD_KEY) == {"ok": False, "message": da.MSG_CS_SAME_KEY_NEW_HOST}
    assert not any(r.headers.get("authorization") for r in srv.requests)


def test_switch_saves_both_in_one_call(rig):
    out = rig.go()
    assert out["ok"] and out["revoke_hint"] == da.MSG_CS_REVOKE
    assert rig.saves == [{"posting_server_url": NEW, "posting_server_api_key": NEW_KEY, "auto_sync_enabled": False}]


def test_switch_save_schedules_no_sync_push(rig):
    rig.go()
    assert rig.saves[0]["auto_sync_enabled"] is False


def test_old_agent_keeps_its_pair(rig):
    rig.go()
    assert (rig.old.server_url, rig.old.api_key) == (OLD, OLD_KEY) and rig.old._stop.is_set()
    new = rig.made[0]
    assert (new.server_url, new.api_key) == (NEW, NEW_KEY) and rig.conn.agent is new


def test_no_agent_while_the_old_page_is_still_up(rig):
    seen = []
    rig.conn._load_html = lambda h: seen.append(("html", rig.conn.agent))
    rig.conn._wait_loaded = lambda: seen.append(("loaded", rig.conn.agent))
    rig.conn._load_url = lambda u: seen.append(("url", rig.conn.agent))
    assert rig.go()["ok"]
    assert seen == [("html", None), ("loaded", None), ("url", rig.made[0])]


def test_form_closed_mid_save_keeps_the_chosen_items(rig):
    _fill(rig.queue)
    rig.conn._save = lambda d: (rig.saves.append(d),
                                threading.Thread(target=rig.conn.form_closed).start())
    assert rig.go(send=True)["ok"]
    assert rig.queue.pending_count() == 3


def test_js_string_cannot_close_the_script():
    page = da.change_server_page("n", "</script><script>alert(1)</script>")
    script = page.split("<script>", 1)[1]
    assert "</script><script>" not in script.rsplit("</script>", 1)[0]


def test_switch_order(rig):
    rig.go()
    kinds = [k for k, _ in rig.nav]
    assert kinds == ["html", "url"] and "Switching server" in rig.nav[0][1] and rig.nav[1] == ("url", NEW)


def test_old_reach_watcher_does_not_load_after_a_switch(rig):
    rig.go()
    rig.nav.clear()
    rig.conn.reach_callback(rig.old)
    assert rig.nav == []
    rig.conn.reach_callback(rig.conn.agent)
    assert rig.nav == [("url", NEW)]


def test_one_origin_end_to_end(rig, srv):
    n = rig.form()
    r = rig.conn.cs_check(n, "https://faß.example")
    assert rig.conn.cs_connect(n, NEW_KEY, True)["ok"]
    origin = "https://xn--fa-hia.example"
    assert r["to_host"] == "xn--fa-hia.example"
    assert {q.url.raw_host.decode() for q in srv.requests} == {"xn--fa-hia.example"}
    assert rig.saves[0]["posting_server_url"] == origin and rig.nav[-1] == ("url", origin)


def test_connect_from_unbound_page(tmp_path, srv):
    r = Rig(tmp_path, agent=False)
    r.conn.unbound_html()
    out = r.go()
    assert out["ok"] and len(r.saves) == 1 and r.conn.agent is r.made[0]


def test_switch_copes_with_window_gone(rig):
    def gone(*a):
        raise RuntimeError("window destroyed")
    rig.conn._load_url = rig.conn._load_html = gone
    assert rig.go()["ok"] and rig.conn.agent is rig.made[0]


def test_key_never_returned_or_logged(rig, srv, caplog):
    caplog.set_level(logging.DEBUG)
    outs = [rig.check()]
    n = rig.conn.form_nonce
    outs.append(rig.conn.cs_connect(n, "pp_new\nkey_5678", True))
    outs.append(rig.conn.cs_connect(n, "pp_wrong_key_0", True))
    outs.append(rig.conn.cs_connect(n, NEW_KEY, True))
    outs.append(rig.conn.agent_status())
    blob = json.dumps(outs) + caplog.text
    for k in (NEW_KEY, "pp_wrong_key_0", "key_5678", OLD_KEY):
        assert k not in blob


def test_reopen_uses_new_server(rig):
    rig.go()
    saved = {"setup_mode": "connected", **rig.saves[0]}
    assert da.connect_target([], saved) == (NEW, NEW_KEY)


# ── T016: waiting items ──────────────────────────────────────────────────────

def _fill(q):
    a = q.add("cookies", {"platform": "fa", "cookies": {"a": "1"}, "account_id": 7}, dedup_key="a", server=OLD)
    b = q.add("cookies", {"platform": "zz_unknown", "cookies": {}, "account_id": None}, dedup_key="b", server=OLD)
    c = q.add("upload", {"path": "x.png", "kind": "inbox"}, dedup_key="c", server=OLD)
    q.failed(c, "HTTP 503", server=OLD)
    return a, b, c


def test_waiting_listed_by_site_and_account(rig):
    _fill(rig.queue)
    w = rig.check()["waiting"]
    assert w["signins"] == [{"site": "FurAffinity", "account": "account 7"},
                            {"site": "a site", "account": "main account"}]
    assert w["uploads"] == 1


def test_discard_removes_only_listed_items(rig):
    _fill(rig.queue)
    assert rig.go(send=False)["ok"]
    assert rig.queue.pending_count() == 0


def test_send_retags_and_resets_backoff(rig):
    _fill(rig.queue)
    assert rig.go(send=True)["ok"]
    items = rig.queue.pending()
    assert len(items) == 3 and {i["server"] for i in items} == {da.server_tag(NEW)}
    assert all(i["attempts"] == 0 and i["next_at"] == 0 for i in items)


def test_helper_queue_untouched(rig, tmp_path):
    rig.go()
    assert not (tmp_path / "helper_queue.json").exists()


# ── T017: pages ──────────────────────────────────────────────────────────────

def _script_and_markup(page: str) -> tuple[str, str]:
    markup, _, rest = page.partition("<script>")
    return markup, rest


def test_form_html_is_self_contained(rig):
    page = rig.conn.form_html()
    markup, script = _script_and_markup(page)
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<a " not in page and "src=" not in markup and "href=" not in page
    assert "http://" not in markup and "https://" not in markup.replace('placeholder="https://"', "")
    assert "dragover" in script and "drop" in script and "innerHTML" not in script
    assert 'type="password"' in markup and "$('key').value=''" in script
    assert rig.conn.form_nonce in script and rig.conn.form_nonce not in markup


def test_current_host_is_escaped(tmp_path, srv):
    r = Rig(tmp_path)
    r.conn.current_url = 'https://x.example/"><script>alert(1)</script>'
    page = r.conn.form_html()
    assert "<script>alert" not in page and "alert(1)" not in page


def test_offline_pages_have_change_server_button(rig):
    for make in (rig.conn.offline_html, rig.conn.unbound_html):
        page = make()
        markup, script = _script_and_markup(page)
        assert "Change server…" in markup and "Discord" not in page
        assert rig.conn.offline_nonce in script and rig.conn.offline_nonce not in markup
    rig.old.last_error = "relay"
    assert "Change server…" in rig.conn.offline_html()


def test_unbound_page_shown_without_any_request(tmp_path, srv):
    r = Rig(tmp_path, agent=False)
    assert "Connect this app" in r.conn.unbound_html() and srv.requests == []


# ── T022/T023: refusals change nothing ───────────────────────────────────────

@pytest.mark.parametrize("case", ["unreachable", "relay", "not_pawpoller", "wrong_key", "unsafe", "empty"])
def test_refusals_change_nothing(rig, srv, monkeypatch, case):
    _fill(rig.queue)
    before = [dict(i) for i in rig.queue.pending()]
    address, key = NEW, NEW_KEY
    if case == "unreachable":
        def boom():
            def h(request):
                raise httpx.ConnectError("down")
            return httpx.Client(transport=httpx.MockTransport(h))
        monkeypatch.setattr(da, "helper_client", boom)
    elif case == "relay":
        srv.health = (200, {"status": "ok", "mode": "relay", "version": "1"})
    elif case == "not_pawpoller":
        srv.raw = (200, b"<html>", {})
    elif case == "wrong_key":
        key = "pp_wrong_key_0"
    elif case == "unsafe":
        address = "http://203.0.113.9"
    elif case == "empty":
        address = ""
    n = rig.form()
    r = rig.conn.cs_check(n, address)
    if r["ok"]:
        r = rig.conn.cs_connect(n, key, True)
    assert r["ok"] is False and r["message"]
    assert rig.saves == [] and rig.conn.agent is rig.old and rig.nav == []
    assert [dict(i) for i in rig.queue.pending()] == before and not rig.old._stop.is_set()


def test_cancel_during_connect_saves_nothing(rig, srv, monkeypatch):
    n = rig.form()
    assert rig.conn.cs_check(n, NEW)["ok"]
    gate, entered = threading.Event(), threading.Event()
    real = da.check_key

    def slow(origin, key):
        entered.set()
        gate.wait(5)
        return real(origin, key)
    monkeypatch.setattr(da, "check_key", slow)
    out = {}
    t = threading.Thread(target=lambda: out.update(rig.conn.cs_connect(n, NEW_KEY, True)))
    t.start()
    assert entered.wait(5)
    assert rig.conn.cs_cancel(n) is True and rig.closed == 1          # not BUSY while connect runs
    gate.set()
    t.join(5)
    assert out["ok"] is False and rig.saves == [] and rig.conn.agent is rig.old


# ── T012/T018/T025: main.py wiring, against a stand-in webview ──────────────

class _Ev:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, f):
        self.handlers.append(f)
        return self

    def fire(self):
        for h in list(self.handlers):
            h()


class _Win:
    def __init__(self, title, url=None, html=None, **kw):
        self.title, self.url, self.html, self.kw = title, url, html, kw
        self.exposed, self.loaded, self.scripts = [], [], []
        self.destroyed = self.shown = False
        self.events = types.SimpleNamespace(closing=_Ev(), closed=_Ev(), loaded=_Ev())

    def expose(self, *fns):
        self.exposed += [f.__name__ for f in fns]

    def load_url(self, u):
        self.loaded.append(u)

    def load_html(self, h):
        self.loaded.append("html")

    def show(self):
        self.shown = True

    def hide(self):
        pass

    def destroy(self):
        self.destroyed = True
        self.events.closed.fire()

    def evaluate_js(self, code):
        self.scripts.append(code)


class _Icon:
    visible = False

    def run(self, **kw):
        pass

    def stop(self):
        pass


@pytest.fixture
def harness(monkeypatch, tmp_path, srv):
    import main
    created: list[_Win] = []
    fake = types.ModuleType("webview")
    fake.settings = {}
    fake.windows = []

    def create_window(title, url=None, html=None, **kw):
        w = _Win(title, url=url, html=html, **kw)
        created.append(w)
        return w
    fake.create_window = create_window
    fake.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, "webview", fake)
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", webbrowser.open)   # run_connected guards it; restore after
    monkeypatch.setattr(da, "retire_local_database", lambda *a, **k: [])
    monkeypatch.setattr(da.Agent, "server_reachable", lambda self: False)
    monkeypatch.setattr(da.Agent, "when_reachable", lambda self, cb, interval=0: None)
    menus = []
    monkeypatch.setattr(main, "_create_tray_icon", lambda helper=False, connected=False: menus.append(connected) or _Icon())
    settings = {"setup_mode": "connected", "posting_server_url": OLD, "posting_server_api_key": OLD_KEY}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    for name in ("_conn", "_form_window", "_window", "_tray_icon"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "_tray_always", False)
    return types.SimpleNamespace(main=main, created=created, menus=menus, settings=settings)


def test_run_connected_windows_have_no_js_api(harness):
    m = harness.main
    m.run_connected(OLD, OLD_KEY)
    main_win = harness.created[0]
    assert "js_api" not in main_win.kw and main_win.html and "Change server" in main_win.html
    assert main_win.exposed == [f.__name__ for f in m._conn.main_functions()]
    assert harness.menus == [True] and m._tray_always is True
    m._open_change_server()
    form = harness.created[1]
    assert "js_api" not in form.kw and form.exposed == ["cs_check", "cs_connect", "cs_cancel"]


def test_tray_open_does_not_script_the_main_window(harness):
    m = harness.main
    m.run_connected(OLD, OLD_KEY)
    m._open_change_server()
    assert harness.created[0].scripts == []


def test_one_form_at_a_time(harness):
    m = harness.main
    m.run_connected(OLD, OLD_KEY)
    m._open_change_server()
    m._open_change_server()
    assert len(harness.created) == 2 and harness.created[1].shown


def test_quit_and_main_close_destroy_the_form(harness):
    m = harness.main
    m.run_connected(OLD, OLD_KEY)
    m._open_change_server()
    form = harness.created[1]
    harness.created[0].events.closed.fire()                 # the main window closed
    assert form.destroyed and m._form_window is None
    m._open_change_server()
    form2 = harness.created[2]
    m._window = harness.created[0]
    m._quit_app()
    assert form2.destroyed


def test_unbound_run_sends_nothing(harness, srv):
    m = harness.main
    m.run_connected("https://other.example", "")
    w = harness.created[0]
    assert "Connect this app" in w.html and "other.example" not in w.html
    assert m._conn.agent is None and srv.requests == []
    assert m._conn.current_url == OLD                       # the form names the saved server, not argv


def test_main_routes_a_mismatched_pair_to_connected_without_a_key(harness, monkeypatch):
    m = harness.main
    monkeypatch.setitem(sys.modules, "update_gate", types.SimpleNamespace(run=lambda: "skipped"))
    calls = []
    monkeypatch.setattr(m, "run_connected", lambda url, key: calls.append((url, key)))
    monkeypatch.setattr(m, "run_standalone", lambda: calls.append("standalone"))
    monkeypatch.setattr(sys, "argv", ["PawPoller", "--connect", "https://other.example"])
    m.main()
    assert calls == [("https://other.example", "")]
    monkeypatch.setattr(sys, "argv", ["PawPoller"])
    m.main()
    assert calls[-1] == (OLD, OLD_KEY)


def test_only_web_and_mail_links_reach_the_system(monkeypatch):
    import webbrowser
    import main
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    main._guard_external_links()
    main._guard_external_links()                 # a second call doesn't wrap it twice
    for bad in ("file:///C:/Windows/System32/calc.exe", "search-ms:query=x", "ms-settings:",
                "javascript:alert(1)", r"\\host\share\x.exe", "  FILE:///etc/passwd", r"C:\x.exe"):
        assert webbrowser.open(bad) is False, bad
    for good in ("https://example.com/a", "HTTP://example.com", "mailto:owner@example.com"):
        assert webbrowser.open(good) is True
    assert opened == ["https://example.com/a", "HTTP://example.com", "mailto:owner@example.com"]
    assert webbrowser.open_new_tab("file:///x") is False


def test_both_window_paths_guard_links():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    for fn in ("def run_connected(", "def run_standalone("):
        assert "_guard_external_links()" in src.split(fn, 1)[1].split("\ndef ", 1)[0]
