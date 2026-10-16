"""FurAffinity bot-check stand-down (4.77.6, spec 040).

FA's Cloudflare challenged every scripted request from a home address. PawPoller's cookie check read the
challenge as "not logged in", so every poll said "FA cookies expired" and good cookies were pasted again for days.
These pin: a bot check is named as one (never an expired login), scheduled polling stands down with a doubling
wait, a good answer ends it, and the wording is one rate watch counts as *challenged*.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

import config
from clients.fa import client as fa_client
from clients.fa.client import FAChallenged, FAClient, _check_challenge, _is_challenge
from polling import fa_standdown

CHALLENGE_HTML = ('<html><head><title>Just a moment...</title></head><body>Checking your browser'
                  '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>')
LOGGED_IN = '<html><body><a href="/logout/?key=1">Log Out</a></body></html>'


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # The skip record is in-memory: without this, a later test's health check sees FA as "skipped".
    monkeypatch.setattr("polling.last_check._skips", {})
    monkeypatch.setattr("polling.fa_poller._fa_skip_recorded_until", 0.0)
    sent = []

    async def _send(text):
        sent.append(text)
        return True
    monkeypatch.setattr("polling.telegram.send_telegram", _send)
    return sent


def _resp(status=200, text="", headers=None):
    h = {"content-type": "text/html; charset=UTF-8", **(headers or {})}
    return httpx.Response(status, text=text, headers=h,
                          request=httpx.Request("GET", "https://www.furaffinity.net/"))


def _client_answering(monkeypatch, handler, calls=None):
    """An FAClient whose direct FA requests go to *handler* through the real response hook."""
    def _h(req):
        if calls is not None:
            calls.append(str(req.url))
        return handler(req)

    async def _get(self):
        if self._fa_http is None:
            self._fa_http = httpx.AsyncClient(transport=httpx.MockTransport(_h),
                                              event_hooks={"response": [_check_challenge]})
        return self._fa_http
    monkeypatch.setattr(FAClient, "_get_fa_http", _get)


# ── Detection ────────────────────────────────────────────────

def test_challenge_header_is_a_bot_check():
    assert _is_challenge(_resp(403, "x", {"cf-mitigated": "challenge"}))


def test_just_a_moment_page_served_with_200_is_a_bot_check():
    assert _is_challenge(_resp(200, CHALLENGE_HTML))


def test_plain_403_is_not_a_bot_check():
    """FA refusing an account itself keeps today's handling."""
    assert not _is_challenge(_resp(403, "<html><title>Forbidden</title></html>"))


def test_message_names_cloudflare_for_rate_watch():
    from polling.rate_watch import classify
    msg = str(FAChallenged("14:05"))
    assert "login is fine" in msg and "14:05" in msg
    assert classify("error", msg) == "challenged"


def test_validate_cookies_does_not_call_a_bot_check_expired(monkeypatch):
    _client_answering(monkeypatch, lambda r: httpx.Response(403, text=CHALLENGE_HTML,
                                                            headers={"cf-mitigated": "challenge"}))
    c = FAClient(username="SecondFur", cookie_a="a", cookie_b="b")
    with pytest.raises(FAChallenged):
        asyncio.run(c.validate_cookies())
    with pytest.raises(FAChallenged):
        asyncio.run(FAClient(username="SecondFur", cookie_a="a", cookie_b="b").validate_session())


# ── Stand-down state ─────────────────────────────────────────

def test_wait_doubles_to_the_ceiling_and_survives_a_restart():
    waits = []
    for _ in range(7):
        fa_standdown.note_challenge()
        waits.append(json.loads((config.DATA_DIR / "fa_standdown.json").read_text())["wait"])
    assert waits[:5] == [7200, 14400, 28800, 57600, 86400]
    assert max(waits) == fa_standdown.CEILING_SECONDS
    assert fa_standdown.remaining() > 86000          # read back from the file, as after a restart


def test_good_answer_lifts_the_pause_but_keeps_the_wait(monkeypatch, _data_dir):
    fa_standdown.note_challenge()
    _client_answering(monkeypatch, lambda r: httpx.Response(200, text=LOGGED_IN))
    assert asyncio.run(FAClient(username="SecondFur", cookie_a="a", cookie_b="b").validate_cookies())
    assert fa_standdown.remaining() == 0
    assert _data_dir == []                               # no "reachable again" for one good page
    fa_standdown.note_challenge()                        # the next page is challenged: the wait doubles
    assert json.loads((config.DATA_DIR / "fa_standdown.json").read_text())["wait"] == 14400


def test_a_clean_poll_forgets_it_and_says_so_once(_data_dir):
    fa_standdown.note_challenge()
    assert asyncio.run(fa_standdown.reset()) is True
    assert len(_data_dir) == 1 and "reachable again" in _data_dir[0]
    assert asyncio.run(fa_standdown.reset()) is False
    fa_standdown.note_challenge()
    assert json.loads((config.DATA_DIR / "fa_standdown.json").read_text())["wait"] == 7200


def test_a_submission_titled_just_a_moment_is_not_a_bot_check():
    """FA titles a page after the piece; a post landing on it must not read as blocked (duplicate-post risk)."""
    page = "<html><head><title>Just a moment... by SecondFur -- Fur Affinity [dot] net</title></head></html>"
    assert not _is_challenge(_resp(200, page))
    assert not _is_challenge(_resp(200, "<html><head><title>Just a moment...</title></head></html>"))


def test_damaged_file_cannot_hold_fa_off_past_the_ceiling():
    (config.DATA_DIR / "fa_standdown.json").write_text(json.dumps({"until": 1e15, "wait": "x"}))
    assert fa_standdown.remaining() <= fa_standdown.CEILING_SECONDS + 1
    assert fa_standdown.next_try_text()
    fa_standdown.note_challenge()                        # non-numeric wait: starts over, doesn't raise


def test_challenge_mid_batch_stops_the_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "FA_REQUEST_DELAY_SECONDS", 0)
    _client_answering(monkeypatch, lambda r: httpx.Response(403, text=CHALLENGE_HTML,
                                                            headers={"cf-mitigated": "challenge"}), calls)
    with pytest.raises(FAChallenged):
        asyncio.run(FAClient(username="SecondFur", cookie_a="a", cookie_b="b")
                    .get_submission_details_batch_direct([1, 2, 3]))
    assert len(calls) == 1


def test_corrupt_state_file_means_no_stand_down():
    (config.DATA_DIR / "fa_standdown.json").write_text("{not json")
    assert fa_standdown.remaining() == 0


# ── Poller ───────────────────────────────────────────────────

def _fa_settings():
    config.save_settings({"fa_username": "SecondFur", "fa_cookie_a": "a", "fa_cookie_b": "b",
                          "telegram_error_alerts": False})


def test_poll_meets_bot_check_stands_down_then_skips_without_a_request(monkeypatch):
    from polling import fa_poller
    _fa_settings()
    calls = []
    _client_answering(monkeypatch, lambda r: httpx.Response(403, text=CHALLENGE_HTML,
                                                            headers={"cf-mitigated": "challenge"}), calls)
    with pytest.raises(FAChallenged) as exc:
        asyncio.run(fa_poller.run_fa_poll_cycle())
    assert "expired" not in str(exc.value).lower() and "around" in str(exc.value)
    assert fa_standdown.remaining() > 7000
    n = len(calls)

    out = asyncio.run(fa_poller.run_fa_poll_cycle())
    assert "bot check" in out["skipped_reason"]
    assert len(calls) == n                              # no request while standing down

    from database.db import get_connection
    conn = get_connection()
    try:
        row = conn.execute("SELECT status, error_message FROM fa_poll_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    from polling.rate_watch import classify
    assert classify(row[0], row[1]) == "challenged"


def test_user_test_button_reports_blocked_and_does_not_lengthen(monkeypatch):
    from database import accounts as adb
    from database.db import get_connection
    from routes.settings_api import test_account_login
    _fa_settings()
    conn = get_connection()
    try:
        aid = adb.get_default_account_id(conn, "fa", create=True)
        conn.commit()
    finally:
        conn.close()
    fa_standdown.note_challenge()
    before = fa_standdown.until()
    _client_answering(monkeypatch, lambda r: httpx.Response(403, text=CHALLENGE_HTML,
                                                            headers={"cf-mitigated": "challenge"}))
    r = asyncio.run(test_account_login(aid))
    assert r["status"] == "blocked" and "Cloudflare" in r["detail"]
    assert fa_standdown.until() == before


def test_a_challenged_post_is_not_retried_automatically():
    """FR-008: FA asks tools to stop, and a retry after a submit that landed would double-post."""
    from posting.manager import _PERMANENT_ERROR_MARKERS
    msg = str(FAChallenged()).lower()
    assert any(m in msg for m in _PERMANENT_ERROR_MARKERS)


def test_with_the_relay_on_a_poll_goes_through_it_despite_the_stand_down(monkeypatch):
    """FARELAY: the stand-down was earned on the home address; the relay switch sends FA polls via the Worker."""
    from polling import fa_poller
    _fa_settings()
    fa_standdown.note_challenge()
    made = {}

    class _Stop(Exception):
        pass

    def _fa(**kw):
        made.update(kw)
        raise _Stop()

    monkeypatch.setattr(fa_poller, "FAClient", _fa)
    config.save_settings({"cf_worker_url": "https://relay.example.workers.dev", "cf_worker_key": "k"})
    out = asyncio.run(fa_poller.run_fa_poll_cycle())               # switch off: still standing down
    assert "bot check" in out["skipped_reason"] and not made

    config.save_settings({"fa_use_cf_proxy": True})
    try:
        with pytest.raises(_Stop):
            asyncio.run(fa_poller.run_fa_poll_cycle())
    finally:   # the stub fails before the cycle's own try/finally, so the lock is still held
        if fa_poller._fa_poll_lock.locked():
            fa_poller._fa_poll_lock.release()
        fa_poller._fa_poll_running = False
    assert made["proxy_url"] == "https://relay.example.workers.dev" and made["proxy_key"] == "k"
