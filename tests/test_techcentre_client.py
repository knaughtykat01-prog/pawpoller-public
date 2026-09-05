"""Tech Centre client (4.10.0) — consent, classification, scrubbing, queue, sending, capture, routes.

Nothing here touches the network: ``techcentre._send`` is replaced with a fake
service that records what it was given and answers like the real one.
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import techcentre as tc
from routes.tech_api import tech_router


class FakeService:
    def __init__(self):
        self.received: list[dict] = []
        self.answer = {"ok": True, "issue_id": 1, "status": "new", "note": "", "fixed_in": "", "new": True}
        self.fail_with: dict | None = None

    def __call__(self, report):
        self.received.append(report)
        if self.fail_with:
            return dict(self.fail_with)
        return dict(self.answer)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    settings: dict = {}
    monkeypatch.setattr(tc.config, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(tc.config, "save_settings", lambda d: settings.update(d))
    monkeypatch.setattr(tc, "PENDING_PATH", tmp_path / "tech_pending.json")
    monkeypatch.setattr(tc, "STATE_PATH", tmp_path / "tech_state.json")
    monkeypatch.setattr(tc, "TECH_CENTRE_URL", "https://tech.example")
    monkeypatch.setattr(tc, "runtime", lambda: "desktop")
    monkeypatch.setattr(tc, "log_tail", lambda max_bytes=0: "12:00 INFO something\n12:01 ERROR boom")
    monkeypatch.setattr(tc, "_handles_cache", (0.0, []))
    monkeypatch.setenv("PAWPOLLER_TECH_CENTRE_THREAD", "0")
    svc = FakeService()
    monkeypatch.setattr(tc, "_send", svc)
    # A TechHandler that another module's import left on the root logger would capture (and
    # dedup against) what these tests log; start every test from a clean root.
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, tc.TechHandler)]:
        root.removeHandler(h)
    tc._handler = None
    yield svc
    for h in [h for h in root.handlers if isinstance(h, tc.TechHandler)]:
        root.removeHandler(h)
    tc._handler = None


def _report(**over):
    kw = dict(kind="exception", where="polling/fa_poller.py:poll_account", error_class="KeyError",
              message="'submissions' for account 42", tb="Traceback...\nKeyError: 'submissions'")
    kw.update(over)
    return tc.build(**kw)


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls,msg,status,expected", [
    ("KeyError", "'submissions'", None, "technical"),
    ("TypeError", "NoneType has no len()", None, "technical"),
    ("httpx.ConnectError", "[Errno 11001] getaddrinfo failed", None, "user"),
    ("ReadTimeout", "", None, "user"),
    ("PermissionError", "[WinError 5] Access is denied", None, "user"),
    ("RuntimeError", "FA cookie expired — log in again", None, "user"),
    ("RuntimeError", "Rate limit hit, retry later", None, "user"),
    ("PlatformResponse", "Meta said no", 500, "technical"),
    ("PlatformResponse", "Meta said no", 403, "user"),
    ("PlatformResponse", "Bad request", 400, "technical"),
    ("OSError", "[Errno 28] No space left on device", None, "user"),
    ("ValueError", "unexpected JSON shape", None, "technical"),
])
def test_classify(cls, msg, status, expected):
    assert tc.classify(cls, msg, status) == expected


# ── scrub + fingerprint ──────────────────────────────────────────────────────

def test_scrub_removes_secrets_paths_handles_and_settings_handles():
    settings = {"fa_username": "inkwolf_art", "ao3_username": "penwright", "fa_cookie_a": "x" * 40, "posting_server_url": "https://x.example"}
    s = tc.scrub("user inkwolf_art at C:\\Users\\someone\\PawPoller failed; token 123456789:AAHfj39sdfkjsdfkjsdf-3sdfsdfsdfsdfsdfs "
                 "mail a@b.co see https://furaffinity.net/user/penwright/ by @somebody", settings=settings)
    assert "inkwolf_art" not in s and "<handle>" in s
    assert "penwright" not in s
    assert "someone" not in s and "<user>" in s
    assert "<bot-token>" in s and "a@b.co" not in s and "@somebody" not in s
    assert "furaffinity.net/…" in s


def test_scrub_clips():
    assert len(tc.scrub("word " * 400, 500)) == 500


def test_fingerprint_matches_service_algorithm():
    a = tc.fingerprint("exception", "polling/fa.poll", "KeyError", "'submissions' for account 42")
    b = tc.fingerprint("exception", "polling/fa.poll", "KeyError", "'submissions' for account 7")
    c = tc.fingerprint("exception", "polling/fa.poll", "ValueError", "'submissions' for account 42")
    assert a == b != c and len(a) == 24
    assert tc.normalise("id 1234 at 0xdeadbeefcafe 'Some Name'") == "id 0 at hex '…'"


def test_build_report_shape():
    r = _report(context={"account_kind": "fa", "count": 3, "name": "@handle"})
    assert r["app"] == "pawpoller" and r["kind"] == "exception" and r["runtime"] == "desktop"
    assert r["install_id"] == tc.install_id() and len(r["install_id"]) == 36
    assert r["context"] == {"account_kind": "fa", "count": 3, "name": "@…"}
    assert r["log_tail"].startswith("12:00") and r["fingerprint"] == tc.fingerprint("exception", r["where"], "KeyError", r["message"])
    assert r["occurred_at"].endswith("Z")


# ── consent + queue ──────────────────────────────────────────────────────────

def test_consent_states():
    assert tc.consent({}) is None and tc.consent({"tech_reports": ""}) is None
    assert tc.consent({"tech_reports": True}) is True and tc.consent({"tech_reports": "false"}) is False


def test_unasked_install_holds_the_first_technical_error_as_a_prompt(_isolated):
    assert tc.enqueue(_report()) == "prompt"
    assert tc.enqueue(_report(message="another")) == "prompt_pending"
    st = tc.status()
    assert st["asked"] is False and st["consent"] is None and st["pending"] == 0
    assert st["prompt"]["error_class"] == "KeyError" and "42" in st["prompt"]["message"]
    assert _isolated.received == []


def test_prompt_always_sends_and_remembers(_isolated):
    tc.enqueue(_report())
    out = tc.resolve_prompt("always")
    assert out["consent"] is True and out["outcome"] == "queued"
    assert tc.consent() is True and tc.status()["prompt"] is None
    assert tc.flush() == 1 and len(_isolated.received) == 1
    assert _isolated.received[0]["error_class"] == "KeyError"
    assert "fingerprint" not in tc.wire_body(_isolated.received[0]) and "install_id" in tc.wire_body(_isolated.received[0])
    assert tc.status()["last"][0]["sent"] is True and tc.status()["last"][0]["status"] == "new"


def test_prompt_once_sends_one_and_asks_again(_isolated):
    tc.enqueue(_report())
    out = tc.resolve_prompt("once")
    assert out["outcome"] == "sent" and tc.consent() is None
    assert len(_isolated.received) == 1
    assert tc.enqueue(_report(message="a second, different one")) == "prompt"


def test_prompt_never_drops_and_silences(_isolated):
    tc.enqueue(_report())
    assert tc.resolve_prompt("never")["consent"] is False
    assert tc.enqueue(_report()) == "off" and tc.status()["pending"] == 0
    assert _isolated.received == []


def test_queue_dedups_per_hour_and_caps(monkeypatch):
    tc.set_consent(True)
    assert tc.enqueue(_report()) == "queued"
    assert tc.enqueue(_report(message="'submissions' for account 99")) == "deduped"
    monkeypatch.setattr(tc, "DEDUP_SECONDS", 0)
    for i in range(60):
        tc.enqueue(_report(message=f"m{i}", where=f"w{i}"))
    assert tc.status()["pending"] == tc.MAX_PENDING
    assert json.loads(tc.PENDING_PATH.read_text(encoding="utf-8"))[-1]["message"] == "m59"


def test_flush_caches_known_issue_and_user_error_stops_sending(_isolated, monkeypatch):
    tc.set_consent(True)
    _isolated.answer = {"ok": True, "issue_id": 5, "status": "fixed", "note": "Update to 4.10.1", "fixed_in": "4.10.1", "new": False}
    tc.enqueue(_report())
    assert tc.flush() == 1
    fp = _report()["fingerprint"]
    assert tc.known(fp)["fixed_in"] == "4.10.1"
    assert tc.status()["last"][0]["note"] == "Update to 4.10.1"
    # operator later marks it user_error → the client stops sending it
    _isolated.answer = {"ok": True, "issue_id": 5, "status": "user_error", "note": "Re-enter the cookie", "fixed_in": "", "new": False}
    monkeypatch.setattr(tc, "DEDUP_SECONDS", 0)
    tc.enqueue(_report())
    tc.flush()
    assert tc.enqueue(_report()) == "user_error"
    assert tc.status()["pending"] == 0 and len(_isolated.received) == 2


def test_flush_backs_off_on_network_and_429_and_drops_on_400(_isolated, monkeypatch):
    tc.set_consent(True)
    monkeypatch.setattr(tc, "DEDUP_SECONDS", 0)
    tc.enqueue(_report())
    _isolated.fail_with = {"ok": False, "status": 0, "error": "ConnectError"}
    assert tc.flush() == 0 and tc.status()["pending"] == 1
    st = tc._state()
    assert st["failures"] == 1 and st["backoff_until"] > time.time() + 30
    assert tc.flush() == 0 and len(_isolated.received) == 1          # inside the backoff: no attempt
    st["backoff_until"] = 0
    tc._save_state(st)
    _isolated.fail_with = {"ok": False, "status": 429, "error": "slow down"}
    assert tc.flush() == 0 and tc._state()["backoff_until"] > time.time() + 500
    st = tc._state(); st["backoff_until"] = 0; tc._save_state(st)
    _isolated.fail_with = {"ok": False, "status": 400, "error": "bad"}
    assert tc.flush() == 0 and tc.status()["pending"] == 0            # dropped, not retried forever
    assert tc.status()["last"][0]["sent"] is False


def test_flush_needs_consent(_isolated):
    tc.enqueue(_report())            # held as a prompt
    assert tc.flush() == 0 and _isolated.received == []


def test_disabled_build_never_queues(monkeypatch):
    monkeypatch.setattr(tc, "TECH_CENTRE_URL", "")
    tc.set_consent(True)
    assert tc.enqueue(_report()) == "disabled" and tc.status()["enabled"] is False


# ── capture from logging ─────────────────────────────────────────────────────

def _log_with_exc(logger, exc, msg="Poll failed", **extra):
    try:
        raise exc
    except Exception:
        logger.error(msg, exc_info=True, extra=extra)


def test_handler_captures_technical_exceptions_only(monkeypatch):
    tc.set_consent(True)
    log = logging.getLogger("polling.fa_poller")
    h = tc.TechHandler()
    log.addHandler(h)
    try:
        _log_with_exc(log, KeyError("submissions"))
        import httpx
        _log_with_exc(log, httpx.ConnectError("dns"))
        log.error("plain error without a traceback")
    finally:
        log.removeHandler(h)
    items = tc._load_pending()
    assert len(items) == 1
    r = items[0]
    assert r["error_class"] == "KeyError" and r["message"].startswith("Poll failed | ")
    assert r["where"].startswith("tests/test_techcentre_client.py") or ":" in r["where"]
    assert "Traceback" in r["traceback"]


def test_handler_explicit_platform_response_marker():
    tc.set_consent(True)
    log = logging.getLogger("posting.platforms.instagram")
    h = tc.TechHandler()
    log.addHandler(h)
    try:
        log.error("Instagram media container failed: %s", "server error", extra={"tech": {"status": 500, "platform": "ig", "where": "posting/platforms/instagram.py:publish"}})
        log.error("Instagram said forbidden", extra={"tech": {"status": 403, "platform": "ig"}})
    finally:
        log.removeHandler(h)
    items = tc._load_pending()
    assert len(items) == 1 and items[0]["kind"] == "platform_response" and items[0]["platform"] == "ig"
    assert items[0]["where"] == "posting/platforms/instagram.py:publish"


def test_handler_never_recurses_or_raises(monkeypatch):
    tc.set_consent(True)
    monkeypatch.setattr(tc, "capture_record", lambda rec: (_ for _ in ()).throw(RuntimeError("boom")))
    log = logging.getLogger("x.y")
    h = tc.TechHandler()
    log.addHandler(h)
    try:
        _log_with_exc(log, ValueError("v"))       # must not raise
    finally:
        log.removeHandler(h)


def test_install_is_idempotent(monkeypatch):
    root = logging.getLogger()
    before = len(root.handlers)
    tc.install()
    tc.install()
    added = [h for h in root.handlers if isinstance(h, tc.TechHandler)]
    try:
        assert len(added) == 1 and len(root.handlers) == before + 1
    finally:
        for h in added:
            root.removeHandler(h)
        tc._handler = None


def test_report_exception_helper_and_update_kind():
    tc.set_consent(True)
    try:
        raise ValueError("bad manifest")
    except ValueError as e:
        assert tc.report_exception("update_gate.run", e, kind="update") == "queued"
    r = tc._load_pending()[0]
    assert r["kind"] == "update" and r["where"] == "update_gate.run" and r["error_class"] == "ValueError"
    assert tc.report("update", "update_gate.run", "UpdateFailed", "failed: download timed out") == "user"


def test_samples_one_per_kind(_isolated):
    kinds = [k for k in tc.KINDS]
    assert set(tc.SAMPLES) == set(kinds)
    fps = set()
    for k in kinds:
        r = tc.sample_report(k)
        assert r["kind"] == k and r["log_tail"] == "" and r["error_class"]
        assert (r["note"] == tc.SAMPLE_NOTE) == (k != "test")
        fps.add(r["fingerprint"])
    assert len(fps) == len(kinds), "every sample must land in its own issue"
    with pytest.raises(ValueError):
        tc.sample_report("nope")
    res = tc.send_sample("api_500")
    assert res["ok"] and _isolated.received[-1]["kind"] == "api_500" and _isolated.received[-1]["note"] == tc.SAMPLE_NOTE
    assert tc.status()["last"][0]["kind"] == "api_500"


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(tech_router)
    return TestClient(app)


def test_routes_status_consent_prompt(client, _isolated):
    s = client.get("/api/tech/status").json()
    assert s["asked"] is False and s["enabled"] is True and s["install_id"]
    tc.enqueue(_report())
    assert client.get("/api/tech/status").json()["prompt"]["error_class"] == "KeyError"
    assert client.post("/api/tech/prompt", json={"decision": "maybe"}).status_code == 400
    out = client.post("/api/tech/prompt", json={"decision": "always"}).json()
    assert out["consent"] is True and out["status"]["prompt"] is None
    assert client.post("/api/tech/flush", json={}).json()["sent"] == 1
    assert client.post("/api/tech/consent", json={"value": False}).json()["consent"] is False
    assert client.post("/api/tech/consent", json={}).status_code == 400


def test_routes_frontend_error_and_test_report(client, _isolated):
    tc.set_consent(True)
    r = client.post("/api/tech/frontend-error", json={"message": "Cannot read properties of undefined (reading 'x')",
                                                      "source": "https://host/js/app.js", "line": 1234, "stack": "TypeError: ...", "page": "#/library"})
    assert r.json()["outcome"] == "queued"
    q = tc._load_pending()[0]
    assert q["kind"] == "frontend" and q["where"] == "frontend:app.js:1234" and q["context"]["page"] == "#/library"
    assert client.post("/api/tech/frontend-error", json={}).status_code == 400
    # a user-fixable browser error (offline) is not queued
    r = client.post("/api/tech/frontend-error", json={"message": "Failed to fetch: network is unreachable"})
    assert r.json()["outcome"] == "user"
    t = client.post("/api/tech/test", json={})
    assert t.status_code == 200 and _isolated.received[-1]["kind"] == "test" and _isolated.received[-1]["log_tail"] == ""
    _isolated.fail_with = {"ok": False, "status": 503, "error": "down"}
    assert client.post("/api/tech/test", json={}).status_code == 502
    _isolated.fail_with = None
    assert client.post("/api/tech/sample", json={"kind": "bogus"}).status_code == 400
    r = client.post("/api/tech/sample", json={"kind": "frontend"})
    assert r.status_code == 200 and _isolated.received[-1]["kind"] == "frontend"
    _isolated.fail_with = {"ok": False, "status": 503, "error": "down"}
    assert client.post("/api/tech/sample", json={"kind": "update"}).status_code == 502
