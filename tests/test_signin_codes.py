"""Sign in for a server (spec 059): one-time codes, the hand-over, and the desktop half.

Each test names the Security Design row it holds (specs/059-sign-in-inside-app/plan.md).
No pywebview, no network, no real settings: accounts, the site checks and the vault key
are stand-ins.
"""
from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import desktop_agent as da
from auth import browser_login as bl
from routes import settings_api
from routes import signin_helper_api as sh

ACCOUNTS = {
    3: {"account_id": 3, "platform": "fa", "is_default": 0, "label": "Inkwolf", "handle": "inkwolf"},
    4: {"account_id": 4, "platform": "tw", "is_default": 0, "label": "Penwright", "handle": "penwright"},
    5: {"account_id": 5, "platform": "fa", "is_default": 0, "label": "NoName", "handle": ""},
    7: {"account_id": 7, "platform": "fa", "is_default": 1, "label": "Default", "handle": ""},
}


@pytest.fixture
def store(monkeypatch):
    s = {"acct_3_fa_username": "Inkwolf", "acct_4_tw_target_user": "Penwright", "fa_username": "Default",
         "auth_password_hash": config.hash_password("correct horse")}
    monkeypatch.setattr(config, "get_settings", lambda: dict(s))
    monkeypatch.setattr(config, "save_settings", lambda d: s.update(d))
    return s


@pytest.fixture
def env(monkeypatch, store):
    sh._reset_state()
    never = set()
    saved = []
    monkeypatch.setattr(sh, "_account", lambda aid: dict(ACCOUNTS[int(aid)]) if int(aid) in ACCOUNTS else None)
    monkeypatch.setattr(sh, "_default_account_id", lambda p: 7 if p == "fa" else None)
    monkeypatch.setattr(sh, "_never_post", lambda aid: int(aid) in never)

    async def who(platform, creds, stored):
        return True, stored, ""
    monkeypatch.setattr(sh, "_who", who)
    monkeypatch.setattr(bl, "_save_browser_creds",
                        lambda p, c, a: (bl.check_creds(p, c), saved.append((p, dict(c), a)))[1] or c)
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    app = FastAPI()
    app.include_router(sh.signin_helper_router)
    client = TestClient(app, base_url="https://testserver")
    yield {"api": client, "never": never, "saved": saved, "app": app, "store": store}
    sh._reset_state()


def _code(api, platform="fa", account_id=3, **extra):
    r = api.post("/api/settings/signin-codes", json={"platform": platform, "account_id": account_id, **extra})
    assert r.status_code == 200, r.text
    return r.json()


FA = {"fa_cookie_a": "plaincookieA" * 3, "fa_cookie_b": "plaincookieB" * 3}


# ── the happy path ───────────────────────────────────────────────────────────

def test_make_lookup_result_saves_to_the_codes_account(env):
    api = env["api"]
    c = _code(api)
    assert len(c["code"]) == 6 and set(c["code"]) <= set(sh.CODE_ALPHABET) and c["expires_in"] == 600
    look = api.post("/api/signin-helper/lookup", json={"code": c["code"].lower()})
    assert look.status_code == 200
    assert set(look.json()) == {"platform", "account_label", "allow_http", "expires_in"}
    assert look.json()["platform"] == "fa" and look.json()["allow_http"] is False
    r = api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA})
    assert r.status_code == 200 and r.json() == {"ok": True, "handle": "Inkwolf"}
    assert env["saved"] == [("fa", FA, 3)]
    assert api.get(f"/api/settings/signin-codes/{c['id']}").json() == {"state": "done", "handle": "Inkwolf"}


def test_a_used_or_expired_code_does_nothing(env, monkeypatch):
    api = env["api"]
    c = _code(api)
    assert api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 200
    again = api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA})
    assert again.status_code == 404 and again.json()["detail"] == sh.NOT_VALID
    c2 = _code(api)
    real = sh.time.time
    monkeypatch.setattr(sh.time, "time", lambda: real() + 601)
    assert api.post("/api/signin-helper/lookup", json={"code": c2["code"]}).status_code == 404
    assert len(env["saved"]) == 1


# ── C1: only the site's own sign-in fields ───────────────────────────────────

def test_c1_the_helper_route_refuses_any_other_setting(env):
    api = env["api"]
    for part in ("cookies", "extra_fields"):
        c = _code(api)
        body = {"code": c["code"], "cookies": dict(FA)}
        body[part] = dict(body.get(part) or {}, auth_password_hash="$2b$12$attacker")
        r = api.post("/api/signin-helper/result", json=body)
        assert r.status_code == 400
    assert env["saved"] == [] and env["store"]["auth_password_hash"] != "$2b$12$attacker"


def test_c1_the_connected_desktop_route_refuses_too(monkeypatch, store):
    app = FastAPI()
    app.include_router(settings_api.settings_router)
    api = TestClient(app)
    for part in ("cookies", "extra_fields"):
        body = {"platform": "fa", "cookies": dict(FA)}
        body[part] = dict(body.get(part) or {}, auth_password_hash="$2b$12$attacker")
        r = api.post("/api/settings/browser-login/result", json=body)
        assert r.status_code == 400, r.text
    r = api.post("/api/settings/browser-login/result", json={"platform": "fa", "cookies": {"fa_cookie_a": "x" * 9000}})
    assert r.status_code == 400
    assert store["auth_password_hash"] != "$2b$12$attacker" and "fa_cookie_a" not in store


def test_m_new_2_account_names_in_the_body_are_refused(env):
    api = env["api"]
    c = _code(api)
    r = api.post("/api/signin-helper/result",
                 json={"code": c["code"], "cookies": FA, "extra_fields": {"fa_username": "Stranger"}})
    assert r.status_code == 400
    assert env["store"]["acct_3_fa_username"] == "Inkwolf" and env["saved"] == []


# ── H1 / M-new-3: guessing ───────────────────────────────────────────────────

def test_h1_wrong_codes_from_everyone_void_every_code(env, monkeypatch):
    api = env["api"]
    monkeypatch.setattr(sh, "PER_ADDR", (1000, 600))       # as if from 20 addresses
    live = _code(api)
    for _ in range(20):
        assert api.post("/api/signin-helper/lookup", json={"code": "ZZZZZZ"}).status_code in (404, 429)
    assert api.post("/api/signin-helper/lookup", json={"code": live["code"]}).status_code == 429
    st = api.get(f"/api/settings/signin-codes/{live['id']}").json()
    assert st["state"] == "voided" and st["message"] == sh.VOIDED
    r = api.post("/api/settings/signin-codes", json={"platform": "fa", "account_id": 3})
    assert r.status_code == 429 and r.json()["detail"] == sh.VOIDED


def test_h1_per_address_limit_counts_ipv6_by_64():
    sh._reset_state()
    now = 1000.0
    for i in range(10):
        assert sh._addr_ok(f"2001:db8:1:2::{i + 1}", now)
    assert not sh._addr_ok("2001:db8:1:2:ffff::9", now)       # same /64
    assert sh._addr_ok("2001:db8:1:3::1", now)                # another /64
    assert sh._addr_key("::ffff:192.0.2.7") == "192.0.2.7"
    sh._reset_state()


def test_m_new_3_helper_failures_never_touch_the_dashboard_lockout(env, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, "_auth_failures", {})
    api = env["api"]
    for _ in range(12):
        api.post("/api/signin-helper/lookup", json={"code": "WRONG1"})
    assert dashboard._auth_failures == {}
    assert not dashboard._is_rate_limited("testclient")


# ── M4 / M-r2-1: whose sign-in it is ─────────────────────────────────────────

def test_m4_another_account_or_a_dead_session_saves_nothing(env, monkeypatch):
    api = env["api"]
    answers = iter([(False, "Stranger", ""), (False, "", "FurAffinity didn't accept that sign-in.")])

    async def who(platform, creds, stored):
        return next(answers)
    monkeypatch.setattr(sh, "_who", who)
    c = _code(api)
    r = api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA})
    assert r.status_code == 409 and r.json()["detail"] == "That's signed in as Stranger, not Inkwolf."
    c2 = _code(api)
    r2 = api.post("/api/signin-helper/result", json={"code": c2["code"], "cookies": FA})
    assert r2.status_code == 409 and "didn't accept" in r2.json()["detail"]
    assert env["saved"] == []
    assert api.get(f"/api/settings/signin-codes/{c['id']}").json()["state"] == "failed"


def test_m_r2_1_x_must_be_the_active_account():
    class FakeTW:
        def __init__(self, auth_token, ct0, target_user):
            pass

        async def session_owner(self):
            return list(FakeTW.owners)

        async def close(self):
            pass
    import clients.tw.client as twc
    real = twc.TWClient
    twc.TWClient = FakeTW
    try:
        FakeTW.owners = ["SecondFur", "Penwright"]
        ok, who, _ = asyncio.run(sh._who("tw", {"tw_auth_token": "t", "tw_ct0": "c"}, "Penwright"))
        assert ok is False and who == "SecondFur"
        FakeTW.owners = []
        assert asyncio.run(sh._who("tw", {}, "Penwright"))[0] is False
        FakeTW.owners = ["penwright", "SecondFur"]
        assert asyncio.run(sh._who("tw", {}, "Penwright"))[0] is True
    finally:
        twc.TWClient = real


def test_m4_fa_without_a_reported_name_is_not_ok(monkeypatch):
    import clients.fa.client as fac

    class FakeFA:
        def __init__(self, **kw):
            pass

        async def validate_session(self):
            return {"ok": True, "logged_in": True, "username": "", "matches": True}

        async def close(self):
            pass
    monkeypatch.setattr(fac, "FAClient", FakeFA)
    ok, who, why = asyncio.run(sh._who("fa", FA, "Inkwolf"))
    assert ok is False and "which account" in why


def test_l_r2_4_no_code_without_a_stored_name_unless_the_owner_types_it(env):
    api = env["api"]
    r = api.post("/api/settings/signin-codes", json={"platform": "fa", "account_id": 5})
    assert r.status_code == 400 and r.json()["detail"] == "Enter this account's username first."
    c = _code(api, account_id=5, username="SecondFur")
    assert env["store"]["acct_5_fa_username"] == "SecondFur" and c["code"]


# ── M5: one hand-over per code ───────────────────────────────────────────────

def test_m5_two_hand_overs_at_once_save_once(env, monkeypatch):
    async def slow_who(platform, creds, stored):
        await asyncio.sleep(0.05)
        return True, stored, ""
    monkeypatch.setattr(sh, "_who", slow_who)
    c = _code(env["api"])

    async def both():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
            return await asyncio.gather(*[ac.post("/api/signin-helper/result",
                                                  json={"code": c["code"], "cookies": FA}) for _ in range(2)])
    rs = asyncio.run(both())
    assert sorted(r.status_code for r in rs) == [200, 404]
    assert len(env["saved"]) == 1


def test_m5_a_failed_hand_over_uses_the_code_up(env):
    api = env["api"]
    c = _code(api)
    assert api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": {}}).status_code == 400
    assert api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 404


# ── M6 / L2 / DA ─────────────────────────────────────────────────────────────

def test_m6_never_post_at_creation_and_at_hand_over(env):
    api = env["api"]
    env["never"].add(3)
    assert api.post("/api/settings/signin-codes", json={"platform": "fa", "account_id": 3}).status_code == 403
    env["never"].clear()
    c = _code(api)
    env["never"].add(3)                       # marked Never post while the code was live
    assert api.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 403
    assert env["saved"] == []


def test_m6_a_missing_account_id_names_the_default_account(env):
    c = _code(env["api"], account_id=None)
    assert env["api"].post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 200
    assert env["saved"][0][2] == 7


def test_l2_and_da_get_no_codes(env):
    api = env["api"]
    for plat in ("ao3", "ib", "ws", "sqw", "da", "nope"):
        assert api.post("/api/settings/signin-codes", json={"platform": plat}).status_code == 400, plat


# ── M3 / L3 / L9 ─────────────────────────────────────────────────────────────

def test_m3_the_code_is_never_in_a_path_and_only_two_paths_are_exempt(env):
    import dashboard
    helper_paths = {r.path for r in env["app"].routes if getattr(r, "path", "").startswith("/api/signin-helper")}
    assert helper_paths == {"/api/signin-helper/lookup", "/api/signin-helper/result"}
    assert all("{" not in p for p in helper_paths)
    exempt = {p for p in dashboard._AUTH_EXEMPT_PATHS if "signin" in p or "helper" in p}
    assert exempt == helper_paths
    assert not any("signin" in p or "helper" in p for p in dashboard._AUTH_EXEMPT_PREFIXES)


def test_m1_no_desktop_http_route_anywhere():
    import dashboard
    assert not any(getattr(r, "path", "").startswith("/api/helper") for r in dashboard.app.routes)
    assert not hasattr(da.AgentApi, "helper_signin")


def test_l9_a_big_body_is_refused(env):
    big = json.dumps({"code": "ABCDEF", "cookies": {"fa_cookie_a": "x" * 17_000}})
    r = env["api"].post("/api/signin-helper/result", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


# ── M2 / M-r3-2 / M-r3-3: plain http ─────────────────────────────────────────

def test_m2_server_refuses_a_plain_http_hand_over_while_off(env):
    c = _code(env["api"])
    plain = TestClient(env["app"], base_url="http://testserver", client=("192.168.1.30", 50000))
    r = plain.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA})
    assert r.status_code == 403 and r.json()["detail"] == sh.NEEDS_TLS
    assert env["api"].get(f"/api/settings/signin-codes/{c['id']}").json()["state"] == "failed"   # code voided
    assert env["saved"] == []


def test_m_r3_3_tailscale_peers_are_fine_with_it_off(env):
    c = _code(env["api"])
    ts = TestClient(env["app"], base_url="http://testserver", client=("100.101.2.3", 50000))
    assert ts.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 200


def test_m2_with_it_on_a_home_hand_over_goes_through(env):
    env["store"]["signin_helper_allow_http"] = True
    c = _code(env["api"])
    plain = TestClient(env["app"], base_url="http://testserver", client=("192.168.1.30", 50000))
    assert plain.post("/api/signin-helper/lookup", json={"code": c["code"]}).json()["allow_http"] is True
    assert plain.post("/api/signin-helper/result", json={"code": c["code"], "cookies": FA}).status_code == 200


def test_m_r3_2_the_opt_in_needs_the_password_the_tick_and_no_hosting(env, monkeypatch):
    api = env["api"]
    put = lambda **b: api.put("/api/settings/signin-helper-http", json=b)  # noqa: E731
    assert put(allow=True, password="wrong", accept_risk=True).status_code == 403
    assert put(allow=True, password="correct horse").status_code == 400
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    assert put(allow=True, password="correct horse", accept_risk=True).status_code == 403
    env["store"]["signin_helper_allow_http"] = True             # e.g. a restored backup
    assert sh.allow_http() is False                             # never on a hosted seat
    monkeypatch.delenv("PAWPOLLER_HOSTED")
    env["store"]["signin_helper_allow_http"] = False
    assert put(allow=True, password="correct horse", accept_risk=True).json() == {"allow_http": True}
    assert put(allow=False).json() == {"allow_http": False}
    del env["store"]["auth_password_hash"]
    assert put(allow=True, password="", accept_risk=True).status_code == 400


def test_m_r3_2_the_opt_in_never_syncs():
    assert {"signin_helper_allow_http", "signin_helper_http_ok"} <= config.SYNC_EXCLUDE


# ── H-new-1 / L1: the window keeps only the site's own cookies ───────────────

def _ck(name, value, domain):
    c = SimpleCookie()
    c[name] = value
    if domain:
        c[name]["domain"] = domain
    return c


def test_h_new_1_foreign_cookies_never_leave():
    jar = [_ck("pp_session", "DASH", "127.0.0.1"), _ck("a", "FA_A", ".furaffinity.net"),
           _ck("b", "FA_B", "www.furaffinity.net"), _ck("auth_token", "XTOK", ".x.com"),
           _ck("auth", "DA1", ".deviantart.com"), _ck("evil", "E", "evilfuraffinity.net"),
           _ck("nodomain", "N", "")]
    da_ck = bl.PLATFORM_LOGIN["da"]["extract"](bl._flatten_cookies(jar, "deviantart.com"), "")["da_cookie"]
    assert da_ck == "auth=DA1"
    ng = bl._flatten_cookies(jar, "newgrounds.com")
    assert ng == {} and not bl.PLATFORM_LOGIN["ng"]["success_check"](ng, "https://www.newgrounds.com/")
    fa = bl._flatten_cookies(jar, "furaffinity.net")
    assert fa == {"a": "FA_A", "b": "FA_B"}
    assert bl._flatten_cookies(jar, "x.com") == {"auth_token": "XTOK"}
    assert not bl.PLATFORM_LOGIN["tw"]["success_check"](bl._flatten_cookies([_ck("auth_token", "X", "evil.example")], "x.com"),
                                                        "https://x.com/home")
    assert not bl.PLATFORM_LOGIN["fa"]["success_check"]({"a": "1", "b": "2"}, "https://evil.example/")
    assert bl.PLATFORM_LOGIN["fa"]["success_check"]({"a": "1", "b": "2"}, "https://www.furaffinity.net/")


def test_l1_host_checks_parse_the_host():
    assert not bl.PLATFORM_LOGIN["ib"]["success_check"]({}, "https://evil.example/?inkbunny.net")
    assert not bl.PLATFORM_LOGIN["sqw"]["success_check"]({}, "https://squidgeworld.org.evil.example/")
    assert not bl.PLATFORM_LOGIN["ik"]["success_check"]({}, "https://evil.example/itaku.ee")
    assert not bl.PLATFORM_LOGIN["ik"]["success_check"]({}, "https://itaku.ee/login")
    assert bl.PLATFORM_LOGIN["ik"]["success_check"]({}, "https://itaku.ee/home")


def test_l1_itaku_token_is_checked_and_unquoted():
    tok = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    assert bl._clean_token(tok) == tok
    assert bl._clean_token(f'"{tok}"') == tok
    assert bl._clean_token(None) == "" and bl._clean_token(123) == ""
    assert bl._clean_token("x" * 300) == ""
    assert bl._clean_token("<script>alert(1)</script>") == ""
    assert bl.saved_keys("ik") == {"ik_auth_token"} and bl.identity_keys("ik") == {"ik_target_user"}


def test_save_false_writes_nothing_and_extract_js_runs(monkeypatch):
    """The window with a stand-in pywebview: Itaku's token comes from page storage, and
    ``save=False`` returns it without writing a thing (one 2 s poll)."""
    import sys
    import types
    tok = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    class Win:
        events = types.SimpleNamespace(closed=types.SimpleNamespace(__iadd__=lambda s, f: s))

        def get_cookies(self):
            return [_ck("csrftoken", "c", "itaku.ee")]

        def get_current_url(self):
            return "https://itaku.ee/home"

        def evaluate_js(self, expr):
            assert "localStorage" in expr
            return f'"{tok}"'

        def destroy(self):
            pass

    class Events:
        def __init__(self):
            self.closed = self

        def __iadd__(self, f):
            return self
    w = Win()
    w.events = Events()
    stub = types.SimpleNamespace(windows=[object()], create_window=lambda *a, **k: w)
    monkeypatch.setitem(sys.modules, "webview", stub)
    monkeypatch.setattr(bl, "_save_browser_creds", lambda *a: (_ for _ in ()).throw(AssertionError("saved")))
    creds = bl.login_via_browser("ik", account_id=None, save=False, timeout=10)
    assert creds == {"ik_auth_token": tok}


# ── the desktop half ─────────────────────────────────────────────────────────

class Http:
    """A stand-in server for the desktop: records every request, never the network."""

    def __init__(self, lookup=None, result=None, down=False):
        self.calls = []
        self.lookup = lookup or (200, {"platform": "fa", "account_label": "Inkwolf", "allow_http": False, "expires_in": 600})
        self.result = result or (200, {"ok": True, "handle": "Inkwolf"})
        self.down = down

    def post(self, url, json=None, **kw):
        self.calls.append((url, json, kw))
        if self.down:
            raise httpx.ConnectError("down")
        status, body = self.lookup if url.endswith("/lookup") else self.result

        class R:
            status_code = status

            def json(self_inner):
                return body
        return R()

    def close(self):
        pass


@pytest.fixture
def q(tmp_path):
    return da.Queue(tmp_path / "helper.json", cipher=Fernet(Fernet.generate_key()))


def _run(server="https://box.example", http=None, confirm=lambda t, m: True, login=None, settings=None, **kw):
    opened = []

    def login_fn(platform):
        opened.append(platform)
        return login if login is not None else dict(FA)
    out = da.helper_signin(server, "ABC234", confirm=confirm, login_fn=login_fn, http=http or Http(),
                           settings=settings if settings is not None else {}, **kw)
    return out, opened


def test_desktop_happy_path_and_the_window_comes_from_the_local_table(q):
    http = Http(lookup=(200, {"platform": "fa", "site_name": "Bank", "account_label": "Inkwolf",
                              "allow_http": False, "expires_in": 600}))
    out, opened = _run(http=http, queue=q)
    assert out == {"ok": True, "message": "Connected as Inkwolf. Saved on your server."}
    assert opened == ["fa"]
    sent = http.calls[1][1]
    assert sent == {"code": "ABC234", "cookies": FA}


def test_h2_unknown_site_and_a_declined_or_missing_answer_open_nothing(q):
    out, opened = _run(http=Http(lookup=(200, {"platform": "bank", "account_label": "x"})), queue=q)
    assert out["ok"] is False and opened == []
    for answer in (False, None, "yes", 1):
        out, opened = _run(confirm=lambda t, m, a=answer: a, queue=q)
        assert out == {"ok": False, "message": da.MSG_CANCELLED} and opened == []


def test_h3_helper_requests_carry_no_key():
    c = da.helper_client()
    try:
        assert "authorization" not in {k.lower() for k in c.headers}
        assert c.follow_redirects is False
    finally:
        c.close()
    import inspect
    src = inspect.getsource(da.helper_client)
    assert "verify=True" in src and "Authorization" not in src


def test_identity_fields_from_the_window_are_not_sent(q):
    http = Http()
    _run(http=http, login=dict(FA, fa_username="Stranger"), queue=q)
    assert "fa_username" not in http.calls[1][1]["cookies"]


def test_l8_a_waiting_hand_over_is_encrypted_and_expires(q, tmp_path):
    http = Http(result=(200, {}))
    http.down = False

    class Down(Http):
        def post(self, url, json=None, **kw):
            if url.endswith("/result"):
                self.calls.append((url, json, kw))
                raise httpx.ConnectError("down")
            return super().post(url, json, **kw)
    out, _ = _run(http=Down(), queue=q, now=1000.0)
    assert out["ok"] is True and out["queued"] is True
    text = (tmp_path / "helper.json").read_text(encoding="utf-8")
    for secret in ("ABC234", "box.example", "plaincookie"):
        assert secret not in text

    class Never:
        def post(self, *a, **k):
            raise AssertionError("an expired item must not be sent")
    sender = da.HelperSender(queue=q, http=Never())
    assert sender.drain(now=1000.0 + 601) == 0 and q.pending_count() == 0
    assert "ran out" in sender.last_message


def test_l8_the_sender_delivers_without_a_key(q):
    q.add("helper", {"server": "https://box.example", "code": "ABC234", "cookies": FA, "expires_at": 2000.0})
    http = Http()
    sender = da.HelperSender(queue=q, http=http)
    assert sender.drain(now=1000.0) == 1 and q.pending_count() == 0
    assert http.calls[0][0] == "https://box.example/api/signin-helper/result"
    assert "headers" not in http.calls[0][2]


def test_m2_plain_http_rules(q):
    class Boom(Http):
        def post(self, *a, **k):
            raise AssertionError("no request may be made")
    for server in ("http://203.0.113.5:8420", "http://172.32.0.1", "http://0.0.0.0:8420",
                   "http://[::ffff:8.8.8.8]:8420", "http://nas.lan:8420", "http://127.evil.example"):
        out, opened = _run(server, http=Boom(), queue=q)
        assert out == {"ok": False, "message": da.MSG_TLS} and opened == [], server
    home = "http://192.168.1.20:8420"
    out, opened = _run(home, http=Boom(), queue=q)                   # no yes on this desktop: no request at all
    assert out["needs_http_ok"] is True and opened == []
    yes = {"signin_helper_http_ok": ["http://192.168.1.20:8420"]}
    out, _ = _run(home, queue=q, settings=yes)                       # the server hasn't allowed it
    assert out == {"ok": False, "message": da.MSG_TLS}
    allowed = Http(lookup=(200, {"platform": "fa", "account_label": "Inkwolf", "allow_http": True, "expires_in": 600}))
    seen = {}
    out, opened = _run(home, http=allowed, queue=q, settings=yes,
                       confirm=lambda t, m: seen.update(t=t, m=m) or False)
    assert out["message"] == da.MSG_CANCELLED and opened == []
    assert "without encryption" in seen["m"] and "not on café" in seen["m"]
    down = Http(lookup=allowed.lookup)

    class ResultDown(Http):
        def post(self, url, json=None, **kw):
            if url.endswith("/result"):
                raise httpx.ConnectError("down")
            return down.post(url, json, **kw)
    out, _ = _run(home, http=ResultDown(), queue=q, settings=yes)
    assert out == {"ok": False, "message": da.MSG_AWAY} and q.pending_count() == 0   # never queued


def test_m2_local_names_resolve_once_and_pin():
    home = lambda h, p: [(0, 0, 0, "", ("192.168.1.5", p))]  # noqa: E731
    public = lambda h, p: [(0, 0, 0, "", ("8.8.8.8", p))]  # noqa: E731
    mixed = lambda h, p: [(0, 0, 0, "", ("192.168.1.5", p)), (0, 0, 0, "", ("8.8.8.8", p))]  # noqa: E731
    assert da.home_address("http://nas.local:8420", resolve=home) == "http://192.168.1.5:8420"
    assert da.home_address("http://nas.local:8420", resolve=public) is None
    assert da.home_address("http://nas.local:8420", resolve=mixed) is None
    assert da.home_address("http://[fd12::5]:8420") == "http://[fd12::5]:8420"
    assert da.home_address("https://192.168.1.5") is None


def test_l_r2_1_labels_reach_the_dialog_clean(q):
    seen = {}
    evil = "<br>Verified: home.ts.net‮" + "x" * 500 + "\nline2"
    http = Http(lookup=(200, {"platform": "fa", "account_label": evil, "allow_http": False}))
    _run(http=http, queue=q, confirm=lambda t, m: seen.update(m=m) or False)
    assert "<" not in seen["m"] and ">" not in seen["m"] and "‮" not in seen["m"] and "\n" not in seen["m"]
    assert seen["m"].startswith("Sign in to FurAffinity on box.example for ")
    assert len(da.clean_label(evil)) == 60


def test_set_http_ok_is_per_address(monkeypatch):
    s = {}
    monkeypatch.setattr(config, "get_settings", lambda: dict(s))
    monkeypatch.setattr(config, "save_settings", lambda d: s.update(d))
    assert da.set_http_ok("http://192.168.1.20:8420/", True) == ["http://192.168.1.20:8420"]
    assert da.set_http_ok("http://192.168.1.20:8420", False) == []


# ── L6 / FR-SEC-012: the helper's screens set server text as text, never markup ─

def test_signin_helper_js_never_uses_innerhtml():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "js" / "signin_helper.js").read_text(encoding="utf-8")
    code = "\n".join(l for l in js.splitlines() if not l.lstrip().startswith(("*", "/*", "//")))
    assert "innerHTML" not in code and "insertAdjacentHTML" not in code and "outerHTML" not in code
    assert "signin_helper.js" in (root / "frontend" / "index.html").read_text(encoding="utf-8")


# ── implement-stage review: M4 per site, L7, the queue's expiry cap ──────────

class _Stub:
    def __init__(self, out=None, exc=None, owners=None):
        self.out, self.exc, self.owners = out, exc, owners

    def __call__(self, *a, **k):
        return self

    async def validate_session(self):
        if self.exc:
            raise self.exc
        return self.out

    async def validate_token(self, token):
        return self.out

    async def session_owner(self):
        return self.owners

    async def close(self):
        pass


@pytest.mark.parametrize("platform,module,cls,method_out", [
    ("ng", "clients.ng.client", "NgClient", lambda who: {"username": who, "matches": True}),
    ("ik", "clients.ik.client", "IKClient", lambda who: {"status": "ok", "username": who}),
    ("fa", "clients.fa.client", "FAClient", lambda who: {"logged_in": True, "username": who}),
])
def test_m4_who_refuses_another_or_no_name(monkeypatch, platform, module, cls, method_out):
    import importlib
    mod = importlib.import_module(module)
    for who in ("Stranger", ""):
        monkeypatch.setattr(mod, cls, _Stub(out=method_out(who)))
        ok, _, _ = asyncio.run(sh._who(platform, {}, "Inkwolf"))
        assert ok is False, (platform, who)
    monkeypatch.setattr(mod, cls, _Stub(out=method_out("Inkwolf")))
    assert asyncio.run(sh._who(platform, {}, "Inkwolf"))[0] is True


def test_m4_fa_bot_check_is_not_ok(monkeypatch):
    import clients.fa.client as fac
    monkeypatch.setattr(fac, "FAClient", _Stub(exc=fac.FAChallenged("check")))
    ok, who, msg = asyncio.run(sh._who("fa", {}, "Inkwolf"))
    assert ok is False and who == "" and "bot check" in msg


def test_l7_agent_login_never_saves():
    import inspect
    src = inspect.getsource(da.Agent)
    assert "login_via_browser(p, e, account_id=a, save=False)" in src


def test_queued_handover_expiry_is_capped(q):
    class ResultDown(Http):
        def post(self, url, json=None, **kw):
            if url.endswith("/result"):
                raise httpx.ConnectError("down")
            return super().post(url, json, **kw)
    for bad in (10 ** 9, "soon"):
        http = ResultDown(lookup=(200, {"platform": "fa", "account_label": "Inkwolf", "allow_http": False, "expires_in": bad}))
        out, _ = _run(http=http, queue=q, now=1000.0)
        assert out.get("queued") is True
    items = q.pending()
    assert len(items) == 2
    assert all(i["payload"]["expires_at"] <= 1000.0 + da.HELPER_TTL for i in items)
