"""Spec 079 COUNTASK: the better-timed "count this copy" ask.

Asked once, after the install's first successful poll, of every install without a clear answer (an unticked
sign-up box is not one). Nothing is sent before a yes. Consent stays on its own install. httpx is a recorder, so
no test touches the network; the database and settings file are the per-test copies from conftest.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import consent_records
import techcentre as tc
from routes.tech_api import tech_router

ROOT = Path(__file__).resolve().parent.parent
SAME_ORIGIN = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
LEAD = "PawPoller is made by one person. Can it count that this copy is running? No names, nothing you made."
PER_INSTALL = ("consent_records", "tech_reports", "tech_usage", "ig_relay_enabled", "first_poll_ok_at", "usage_ask")


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json})
        import httpx
        return httpx.Response(200, json={"ok": True, "next_in": 300})


@pytest.fixture
def S(tmp_path, monkeypatch):
    """A settings dict behind config.get/save_settings (adult, with a Tech Centre address), plus the recorder."""
    settings: dict = {"setup_mode": "standalone", "age_band": "adult"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(config, "save_settings", lambda d: settings.update(d))
    monkeypatch.setattr(tc, "PENDING_PATH", tmp_path / "tech_pending.json")
    monkeypatch.setattr(tc, "STATE_PATH", tmp_path / "tech_state.json")
    monkeypatch.setattr(tc, "TECH_CENTRE_URL", "https://tech.example")
    monkeypatch.setattr(tc, "runtime", lambda: "desktop")
    monkeypatch.setattr(tc, "_platforms", lambda: ["bsky", "fa"])
    monkeypatch.setattr(tc, "_library", lambda: {"artwork": "11-100"})
    monkeypatch.setenv("PAWPOLLER_TECH_CENTRE_THREAD", "0")
    rec = Recorder()
    import httpx
    monkeypatch.setattr(httpx, "post", rec)
    settings["_rec"] = rec          # handy in tests; never read by the app
    return settings


def _client():
    app = FastAPI()
    app.include_router(tech_router)
    return TestClient(app, headers=SAME_ORIGIN)


def _raw(path, body, headers):
    """POST with exactly these headers (no client defaults)."""
    app = FastAPI()
    app.include_router(tech_router)
    return TestClient(app).post(path, content=json.dumps(body), headers=headers)


def _polled():
    from database.db import get_connection
    conn = get_connection()
    try:
        conn.execute("INSERT INTO ws_poll_log (status) VALUES ('success')")
        conn.commit()
    finally:
        conn.close()


def _rec(S):
    return consent_records.get("tech_usage", S)


# ── Foundational: per install, strict values, the cross-site check ──────────

def test_consent_keys_never_sync():
    config.save_settings({k: v for k, v in zip(PER_INSTALL, [{"tech_usage": {"value": True, "at": "x", "wording": 1}},
                                                               True, True, True, "2026-01-01T00:00:00+00:00",
                                                               {"shown": 1, "last": "2026-01-01"}])})
    pulled, _ = config.get_settings_for_sync()
    assert not set(PER_INSTALL) & set(pulled)
    before = {k: config.get_settings().get(k) for k in PER_INSTALL}
    config.merge_synced_settings({"consent_records": {}, "tech_reports": False, "tech_usage": False,
                                  "ig_relay_enabled": False, "first_poll_ok_at": "", "usage_ask": {"shown": 0}})
    assert {k: config.get_settings().get(k) for k in PER_INSTALL} == before
    from routes import move_api
    assert all(move_api.keeps_here(k) for k in PER_INSTALL)


def test_restore_keeps_consent(tmp_path):
    from routes.backup_api import _keep_local_settings
    live, incoming = tmp_path / "live.json", tmp_path / "in.json"
    live.write_text(json.dumps({"consent_records": {"tech_usage": {"value": True}}}), encoding="utf-8")
    incoming.write_text(json.dumps({"consent_records": {"tech_usage": {"value": False}}, "usage_ask": {"shown": 2},
                                    "tech_usage": False}), encoding="utf-8")
    _keep_local_settings(live, incoming)
    got = json.loads(incoming.read_text("utf-8"))
    assert got["consent_records"] == {"tech_usage": {"value": True}}
    assert "usage_ask" not in got and "tech_usage" not in got      # this install had none: the backup's are dropped


POSTS = [("/api/tech/usage", {"value": True}), ("/api/tech/consent", {"value": True}),
         ("/api/tech/prompt", {"decision": "always"}), ("/api/tech/test", {}), ("/api/tech/sample", {"kind": "test"}),
         ("/api/tech/flush", {}), ("/api/tech/frontend-error", {"message": "x"})]
REFUSED = [
    {"Content-Type": "application/json", "Sec-Fetch-Site": "same-site"},
    {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
    {"Content-Type": "application/json", "Origin": "null"},
    {"Content-Type": "application/json", "Origin": "http://testserver:8421"},
    {"Content-Type": "application/json", "Origin": "http://evil.example"},
    {"Content-Type": "application/json", "Referer": "http://evil.example/page"},
    {"Content-Type": "text/plain", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin"},
    # Implement-stage security review: typed-in/bookmark requests, Sec-Fetch-Site winning over a matching Origin,
    # and JSON with nothing to prove where it came from.
    {"Content-Type": "application/json", "Sec-Fetch-Site": "none"},
    {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
    {"Content-Type": "application/json"},
]
ALLOWED = [SAME_ORIGIN,
           {"Content-Type": "application/json", "Origin": "http://testserver"},
           {"Content-Type": "application/json", "Referer": "http://testserver/#/settings"},
           {"Content-Type": "Application/JSON; charset=utf-8", "Sec-Fetch-Site": "same-origin"}]


@pytest.mark.parametrize("path,body", POSTS)
def test_cross_site_write_refused(S, path, body):
    for h in REFUSED:
        r = _raw(path, body, h)
        assert r.status_code == 403 and r.json()["detail"] == "not_from_dashboard", (path, h, r.status_code)
    assert "consent_records" not in S and "tech_usage" not in S and "tech_reports" not in S
    assert S["_rec"].calls == []
    assert _client().get("/api/tech/status").status_code == 200


def test_allowed_headers_pass_the_check(S):
    for h in ALLOWED:
        r = _raw("/api/tech/usage", {"value": False, "where": "settings"}, h)
        assert r.status_code == 200, (h, r.status_code)


def test_value_must_be_bool(S):
    c = _client()
    for bad in ("false", 0, 1, None):
        assert c.post("/api/tech/usage", json={"value": bad}).status_code == 400
    assert c.post("/api/tech/usage", json={}).status_code == 400
    assert "consent_records" not in S


def test_bad_where_refused(S):
    c = _client()
    for bad in ("bell", 5, ["ask"]):
        assert c.post("/api/tech/usage", json={"value": True, "where": bad}).status_code == 400
    assert "consent_records" not in S
    assert c.post("/api/tech/usage", json={"value": True, "where": None}).status_code == 200
    assert "where" not in _rec(S)


def test_record_stamps_server_fields(S):
    _client().post("/api/tech/usage", json={"value": True, "where": "settings", "at": "1999", "wording": 9,
                                            "app_version": "0.0.1"})
    r = _rec(S)
    assert r["wording"] == consent_records.WORDING["tech_usage"] and r["app_version"] == config.APP_VERSION
    assert r["at"] != "1999" and r["where"] == "settings"
    consent_records.record("tech_reports", True)
    assert "where" not in consent_records.get("tech_reports", S)


def test_no_default_yes(S):
    assert tc.usage_consent() is None
    tc.checkin(force=True)
    assert S["_rec"].calls == []


def test_record_keeps_other_kinds(S, monkeypatch):
    real = config.save_settings
    gate = threading.Barrier(2)

    def slow(d):
        time.sleep(0.05)
        real(d)
    monkeypatch.setattr(config, "save_settings", slow)

    def go(kind, value, where=None):
        gate.wait()
        consent_records.record(kind, value, where)
    ts = [threading.Thread(target=go, args=("tech_reports", True)),
          threading.Thread(target=go, args=("tech_usage", False, "ask"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    recs = S["consent_records"]
    assert recs["tech_reports"]["value"] is True and recs["tech_usage"]["value"] is False


# ── US1: when it's due ───────────────────────────────────────────────────────

def test_not_due_before_first_successful_poll(S):
    assert tc.usage_ask_due() is False
    from database.db import get_connection
    conn = get_connection()
    conn.execute("INSERT INTO ws_poll_log (status) VALUES ('error')")
    conn.commit()
    conn.close()
    assert tc.usage_ask_due() is False


def test_due_after_first_successful_poll(S):
    _polled()
    assert tc.usage_ask_due() is True
    assert S["first_poll_ok_at"]


def test_partial_poll_counts(S):
    # A poll that checked the account but logged 'partial' (a missing Facebook permission, an X rate limit) counts.
    from database.db import get_connection
    conn = get_connection()
    try:
        conn.execute("INSERT INTO ws_poll_log (status) VALUES ('error')")
        conn.commit()
        assert tc.usage_ask_due() is False
        conn.execute("INSERT INTO ws_poll_log (status) VALUES ('partial')")
        conn.commit()
    finally:
        conn.close()
    assert tc.usage_ask_due() is True


def test_first_poll_written_once(S, monkeypatch):
    _polled()
    tc.usage_ask_due()
    stamp, writes = S["first_poll_ok_at"], []
    monkeypatch.setattr(config, "save_settings", lambda d: (writes.append(d), S.update(d)))
    for _ in range(3):
        tc.usage_ask_due()
        tc.status()
    assert S["first_poll_ok_at"] == stamp and not any("first_poll_ok_at" in w for w in writes)


def test_poll_log_lookup_safe(S, monkeypatch):
    from database.db import get_connection
    conn = get_connection()
    conn.execute('CREATE TABLE "x;drop_poll_log" (status TEXT)')
    conn.execute("INSERT INTO \"x;drop_poll_log\" VALUES ('success')")
    conn.execute("CREATE TABLE zz_poll_log (other TEXT)")
    conn.commit()
    conn.close()
    assert tc._first_poll_ok(dict(S)) is False          # the odd name is never queried; zz_ is skipped
    import database.db as dbm

    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(dbm, "get_connection", boom)
    assert tc._first_poll_ok(dict(S)) is False


def test_bad_usage_ask_value_tolerated(S):
    _polled()
    for bad in ("x", {"shown": "a"}, {"shown": 1}):
        S["usage_ask"] = bad
        tc.status()                                      # never raises


def test_never_due_after_any_clear_answer(S):
    _polled()
    for value, where in ((True, "ask"), (False, "ask"), (True, "settings"), (False, "settings"),
                         (True, "signup"), (True, "setup")):
        S.pop("consent_records", None)
        S.pop("usage_ask", None)
        tc.set_usage_consent(value, where)
        assert tc.usage_ask_due() is False, (value, where)


def test_shown_at_most_twice_next_day_only(S, monkeypatch):
    _polled()
    today = date(2026, 11, 8)

    class D(date):
        @classmethod
        def today(cls):
            return today
    monkeypatch.setattr(tc, "date", D)
    assert tc.usage_ask_due() is True
    tc.mark_ask_shown()
    assert tc.usage_ask_due() is False
    today = today + timedelta(days=1)
    assert tc.usage_ask_due() is True
    tc.mark_ask_shown()
    today = today + timedelta(days=30)
    assert tc.usage_ask_due() is False
    assert S["usage_ask"]["shown"] == 2


def test_ask_answer_refused_when_already_answered(S):
    tc.set_usage_consent(False, "settings")
    r = _client().post("/api/tech/usage", json={"value": True, "where": "ask"})
    assert r.status_code == 409 and r.json()["detail"] == "answered"
    assert _rec(S)["value"] is False and _rec(S)["where"] == "settings"


def test_two_tabs_record_one_answer(S, monkeypatch):
    real = consent_records.record
    gate = threading.Barrier(2)

    def slow(*a, **k):
        time.sleep(0.05)
        return real(*a, **k)
    monkeypatch.setattr(consent_records, "record", slow)
    out: list = []

    def answer(v):
        gate.wait()
        try:
            tc.set_usage_consent(v, "ask")
            out.append(("ok", v))
        except tc.AlreadyAnswered:
            out.append(("409", v))
    ts = [threading.Thread(target=answer, args=(v,)) for v in (True, False)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    oks = [v for k, v in out if k == "ok"]
    assert len(oks) == 1 and _rec(S)["value"] is oks[0]

    real_save = config.save_settings

    def slow_save(d):
        time.sleep(0.05)
        real_save(d)
    monkeypatch.setattr(config, "save_settings", slow_save)
    S.pop("usage_ask", None)

    def show():
        gate.wait()
        tc.mark_ask_shown()
    ts = [threading.Thread(target=show) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert S["usage_ask"]["shown"] == 1     # two screens the same day count once; the second showing is a later day


def test_nothing_sent_before_yes(S):
    _polled()
    tc.status()
    tc.usage_ask_due()
    tc.mark_ask_shown()
    tc.checkin_payload()
    tc.set_usage_consent(False, "ask")
    tc.checkin(force=True)
    assert S["_rec"].calls == []
    tc.set_usage_consent(True, "settings")
    tc.checkin(force=True)
    assert len(S["_rec"].calls) == 1


def test_under_18_and_no_age_not_due(S):
    _polled()
    S["age_band"] = "under18"
    assert tc.usage_ask_due() is False
    S.pop("age_band")
    assert tc.usage_ask_due() is False
    S["age_band"] = "adult"
    assert tc.usage_ask_due() is True


def test_no_tech_centre_never_due(S, monkeypatch):
    _polled()
    monkeypatch.setattr(tc, "TECH_CENTRE_URL", "")
    assert tc.usage_ask_due() is False


def test_old_page_field_unchanged(S):
    _polled()
    states = [lambda: None,
              lambda: tc.set_usage_consent(False, "signup"),
              lambda: S.update(consent_records={"tech_usage": {"value": False, "at": "x", "wording": 1}}),
              lambda: tc.set_usage_consent(False, "ask"),
              lambda: tc.set_usage_consent(True, "settings")]
    for put in states:
        S.pop("consent_records", None)
        S.pop("usage_ask", None)
        put()
        st = tc.status()
        assert st["usage_asked"] is (tc.usage_consent() is not None)
    S.pop("consent_records", None)
    tc.set_usage_consent(False, "signup")
    st = tc.status()
    assert st["usage_asked"] is True and st["usage_ask_due"] is True     # an old page shows nothing new


# ── US3: unticked and unknown noes ───────────────────────────────────────────

def test_unticked_signup_is_not_an_answer(S):
    _polled()
    for where in ("signup", "setup"):
        S.pop("consent_records", None)
        tc.set_usage_consent(False, where)
        assert tc.usage_consent() is False and tc.usage_clear() is False and tc.usage_ask_due() is True
        tc.checkin(force=True)
    assert S["_rec"].calls == []


@pytest.mark.parametrize("old", ["record", "flat"])
def test_unknown_no_shown_once(S, monkeypatch, old):
    _polled()
    if old == "record":
        S["consent_records"] = {"tech_usage": {"value": False, "at": "x", "wording": 1}}
    else:
        S["tech_usage"] = False
    today = date(2026, 11, 8)

    class D(date):
        @classmethod
        def today(cls):
            return today
    monkeypatch.setattr(tc, "date", D)
    assert tc.usage_ask_due() is True
    tc.mark_ask_shown()
    today = today + timedelta(days=1)
    assert tc.usage_ask_due() is False


def test_old_wording_yes_not_clear(S):
    S["consent_records"] = {"tech_usage": {"value": True, "at": "x", "wording": 0}}
    assert tc.usage_clear() is False


def test_unticked_cannot_overwrite_clear(S):
    c = _client()
    for v in (True, False):
        S.pop("consent_records", None)
        tc.set_usage_consent(v, "settings")
        r = c.post("/api/tech/usage", json={"value": False, "where": "signup"})
        assert r.status_code == 200 and _rec(S)["value"] is v and _rec(S)["where"] == "settings"


def test_shown_route(S):
    c = _client()
    assert c.post("/api/tech/usage-ask/shown", json={}).json() == {"shown": 1}
    for h in REFUSED:
        assert _raw("/api/tech/usage-ask/shown", {}, h).status_code == 403
    assert S["usage_ask"]["shown"] == 1


# ── US2: every field named; no "anonymous" ───────────────────────────────────

def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).replace("\\'", "'").replace("&#39;", "'").replace("&rsquo;", "'").replace("’", "'").lower()


def test_checkin_fields_all_described(S):
    assert set(tc.checkin_payload()) == set(tc.CHECKIN_FIELDS)
    names = [*tc.CHECKIN_FIELDS.values(), tc.COUNTRY_NOTE]
    # The website's privacy page is checked in tests/test_site_hygiene.py (site/ isn't in the public copy).
    for rel in ("frontend/legal/privacy.html", "docs/legal/privacy_DRAFT.md", "docs/PRIVACY_TEMPLATE.md"):
        text = _norm((ROOT / rel).read_text(encoding="utf-8"))
        missing = [n for n in names if _norm(n) not in text]
        assert not missing, (rel, missing)
    assert tc.status()["checkin_fields"] == names
    assert "random id" in tc.CHECKIN_FIELDS["install_id"] and "country" in tc.COUNTRY_NOTE
    for fn in _render_functions():
        assert "checkin_fields" in fn


def _body(src, start):
    """The function body that starts at `start` (up to the next method at the same indent)."""
    assert start in src, start
    return src.split(start, 1)[1].split("\n    },", 1)[0]


def _render_functions():
    """Each place that describes check-ins renders the one field list (FR-SEC-007), sliced per place."""
    app = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    diag = (ROOT / "frontend/js/diagnostics.js").read_text(encoding="utf-8")
    fill = _body(app, "_fillUsageFields(ul) {")
    signup = app.split("screen === 'tech'", 1)[1].split("/* ── Setup Wizard", 1)[0]
    assert "_fillUsageFields(document.getElementById('signup-usage-fields'))" in signup and 'id="signup-usage-fields"' in signup
    assert "_fillUsageFields(document.getElementById('setup-usage-fields'))" in app and 'id="setup-usage-fields"' in app
    return [_body(app, "async _showUsagePromptModal(st) {"), fill, _body(diag, "_renderTech() {")]


ANON_FILES = ("frontend/js/app.js", "frontend/js/diagnostics.js", "consent_records.py", "frontend/legal/privacy.html",
              "docs/legal/privacy_DRAFT.md", "docs/PRIVACY_TEMPLATE.md")   # the website's pages: test_site_hygiene.py
# Unrelated uses, by exact line text (a site's own "anonymous rate limit", not check-ins).
ANON_ALLOWED = ("it also raises the anonymous rate limit.",)


def test_no_anonymous_wording():
    hits = []
    for rel in ANON_FILES:
        for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"anonymous", line, re.I) and not any(a in line for a in ANON_ALLOWED):
                hits.append(f"{rel}:{i}")
    assert not hits, hits


# ── the page (static checks) ─────────────────────────────────────────────────

def _app():
    return (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")


def test_ask_waits_for_open_dialog():
    guard = _body(_app(), "_noOtherDialog() {")
    assert '[aria-modal="true"]' in guard and "dialog[open]" in guard and "whatsnew-ov" in guard
    # Never over sign-in, sign-up or setup, even before that screen has drawn (the route is checked too).
    assert ".login-screen" in guard and "this._atAuthGate()" in guard
    gate = _body(_app(), "_atAuthGate() {")
    assert "location.hash" in gate
    for route in ("dashboard-login", "signup", "setup", "login"):
        assert f"'{route}'" in gate
    # Never over a page tour (seen in the 079 screenshot run), and checked again after the preview fetch.
    assert "pp-tour-active" in guard
    show = _body(_app(), "async _showUsagePromptModal(st) {")
    assert show.index("this._noOtherDialog()") > show.index("getCheckinPreview()")
    assert show.index("this._noOtherDialog()") < show.index("markUsageAskShown")
    ask = _body(_app(), "async _maybeShowTechPrompt() {")
    assert "usage_ask_due" in ask and "_noOtherDialog()" in ask and "_usageAskShownThisSession" in ask


def test_ask_checked_while_dashboard_open():
    # A dashboard left open sees the ask after the first poll: the 60 s status ticker asks too (gap check).
    tick = _body(_app(), "async _updateStatusCheck() {")
    assert "this._maybeShowTechPrompt()" in tick and "this._atAuthGate()" in tick


def test_whats_new_waits_for_the_ask():
    # What's new never opens over the ask (gap check): it waits while #usageprompt-ov is open.
    wn = _body(_app(), "async _maybeShowWhatsNew() {")
    assert wn.index("usageprompt-ov") < wn.index("this._showWhatsNewModal(data)")


def test_ask_buttons_equal():
    ask = _body(_app(), "async _showUsagePromptModal(st) {")
    assert LEAD in ask
    yes = re.search(r'<button class="([^"]+)"[^>]*data-usage="yes"[^>]*>', ask)
    no = re.search(r'<button class="([^"]+)"[^>]*data-usage="no"[^>]*>', ask)
    assert yes and no and yes.group(1) == no.group(1)
    assert "autofocus" not in ask and "checked" not in ask
    for m in re.finditer(r"([\w$.'\[\]=\"-]+)\.focus\(", ask):
        assert "data-usage" not in m.group(1)
    assert "API.markUsageAskShown()" in ask and "'ask'" in ask and "quiet" in ask


def test_signup_screen_finishes_unticked(S, monkeypatch):
    import dashboard
    from routes.dashboard_auth import dashboard_auth_router
    monkeypatch.setattr(dashboard, "_client_is_loopback", lambda request: True)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    tc.set_usage_consent(False, "signup")
    tc.set_consent(False)
    app = FastAPI()
    app.include_router(dashboard_auth_router)
    out = TestClient(app).get("/api/auth/signup-status").json()
    assert out["answered"]["tech"] is True


def test_every_place_sends_where():
    app = _app()
    diag = (ROOT / "frontend/js/diagnostics.js").read_text(encoding="utf-8")
    assert "API.setTechUsage(usage, 'signup')" in app and "API.setTechUsage(usage, 'setup')" in app
    assert "where: 'settings'" in diag
