"""Spec 089: operator announcements.

What is pinned here: an install shows only what it can prove the operator signed; it says nothing
about itself when it asks; a page request never causes a fetch; closing is remembered and can't be
done by a stranger on a copy with no password; and with no key shipped the whole thing does nothing.

Keys are generated in the test. Nothing here reaches the network: fetches use a stand-in transport.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import announce
import config
import updater
from routes import api

NOW = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)
T = "%Y-%m-%dT%H:%M:%SZ"


def _t(days: float = 0) -> str:
    return (NOW + timedelta(days=days)).strftime(T)


def _item(**over) -> dict:
    return {"id": "sample-notice", "level": "heads-up", "title": "A site is asking everyone to sign in again",
            "body": "Open Platforms and press Reconnect.", "starts": _t(-1), "ends": _t(5), **over}


def _public(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _file(key, items=None, issued=None, key_id="t1", prefix=announce.PREFIX, kind="pawpoller-announcements") -> bytes:
    """A feed file, built the way the Tech Centre builds one (data-model.md)."""
    payload = json.dumps({"kind": kind, "issued_at": issued or _t(), "items": [_item()] if items is None else items}).encode()
    return json.dumps({"v": 1, "key": key_id, "payload": base64.b64encode(payload).decode(),
                       "sig": base64.b64encode(key.sign(prefix + payload)).decode()}).encode()


@pytest.fixture
def key(monkeypatch, tmp_path):
    """A signing key whose public half this copy trusts, and a clean state file and settings."""
    k = Ed25519PrivateKey.generate()
    monkeypatch.setattr(announce, "PUBLIC_KEYS", {"t1": _public(k)})
    monkeypatch.setattr(announce, "_state_path", lambda: tmp_path / "announce_state.json")
    monkeypatch.setattr(announce, "_warned", False)
    config.save_settings({"announcements_enabled": True, "announce_closed": []})
    yield k
    config.delete_settings_keys(["announcements_enabled", "announce_closed"])


def _refused(raw, **kw) -> str:
    with pytest.raises(announce.Refused) as e:
        announce.verify(raw, now=NOW, **kw)
    return e.value.reason


# ── Who can make one ─────────────────────────────────────────────────────────

class TestOnlyTheOperatorsSetIsAccepted:
    def test_a_tech_centre_file_verifies(self, key):
        items, issued = announce.verify(_file(key), now=NOW)
        assert [i["id"] for i in items] == ["sample-notice"] and issued == _t()

    def test_forged_set_is_refused(self, key):
        assert _refused(_file(Ed25519PrivateKey.generate())) == "bad_signature"

    def test_altered_payload_is_refused(self, key):
        doc = json.loads(_file(key))
        payload = json.loads(base64.b64decode(doc["payload"]))
        payload["items"][0]["title"] = "Download the fix from an address"
        doc["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
        assert _refused(json.dumps(doc).encode()) == "bad_signature"

    def test_wrong_prefix_is_refused(self, key):
        """A signature made for something else, even by the right key, is no good here."""
        assert _refused(_file(key, prefix=b"something-else\n")) == "bad_signature"
        assert _refused(_file(key, kind="another-kind")) == "wrong_kind"

    def test_retired_key_is_refused(self, key, monkeypatch):
        raw = _file(key)
        monkeypatch.setattr(announce, "PUBLIC_KEYS", {"t2": _public(Ed25519PrivateKey.generate())})
        assert _refused(raw) == "unknown_key"

    def test_empty_set_is_accepted(self, key):
        assert announce.verify(_file(key, items=[]), now=NOW)[0] == []

    def test_older_set_is_refused(self, key):
        assert _refused(_file(key, issued=_t(-2)), newest=_t(-1)) == "older"
        assert announce.verify(_file(key, issued=_t(-1)), newest=_t(-1), now=NOW)   # the same one again is fine

    def test_future_marker_is_refused(self, key):
        """Or one set marked years ahead would make every later genuine set "older"."""
        assert _refused(_file(key, issued=_t(2))) == "future"
        assert announce.verify(_file(key, issued=_t(0.5)), now=NOW)

    def test_no_setting_or_env_changes_keys_or_url(self, key, monkeypatch):
        forged = _file(Ed25519PrivateKey.generate(), key_id="mine")
        monkeypatch.setenv("PAWPOLLER_ANNOUNCE_KEYS", json.dumps({"mine": "x"}))
        monkeypatch.setenv("PAWPOLLER_ANNOUNCE_URL", "https://example.com/feed.json")
        config.save_settings({"announce_public_keys": {"mine": "x"}, "announce_feed_url": "https://example.com/x"})
        try:
            assert _refused(forged) == "unknown_key"
            seen = []
            announce._http_get(httpx.MockTransport(lambda r: seen.append(str(r.url)) or httpx.Response(200, content=b"{}")))
            assert seen == [announce.FEED_URL]
        finally:
            config.delete_settings_keys(["announce_public_keys", "announce_feed_url"])

    def test_feed_is_not_the_release_repository(self):
        """The credential that can write the feed must not reach what the updater trusts."""
        assert announce.FEED_URL.startswith("https://raw.githubusercontent.com/")
        assert updater.GITHUB_REPO not in announce.FEED_URL
        assert "/pawpoller-announcements/" in announce.FEED_URL

    def test_shipped_keys_are_well_formed(self):
        """A typo in the shipped key would silently refuse everything; fail the suite instead."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        for key_id, public in announce.PUBLIC_KEYS.items():
            assert key_id and len(key_id) <= 20
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))


# ── What one may contain ─────────────────────────────────────────────────────

class TestTheItemsAreStrict:
    @pytest.mark.parametrize("over", [
        {"id": "Has Capitals"}, {"id": ""}, {"id": "a" * 41}, {"id": "under_score"},
        {"level": "critical"}, {"level": "Urgent"},
        {"title": ""}, {"title": "x" * 121}, {"title": "two\nlines"}, {"title": 5},
        {"body": ""}, {"body": "x" * 1001},
        {"starts": "2026-11-16"}, {"starts": "2026-13-40T00:00:00Z"}, {"ends": _t(-2)},
        {"min_version": "4.92.0-beta"}, {"max_version": "latest"},
    ])
    def test_level_id_dates_are_strict(self, key, over):
        assert _refused(_file(key, items=[_item(**over)])) == "bad_item"

    @pytest.mark.parametrize("bad", ["‮", "⁦", "‏", "\u0007", " ", "​", "\r"])
    def test_control_and_bidi_characters_are_refused(self, key, bad):
        """Hidden characters can make text read differently from what was signed."""
        assert _refused(_file(key, items=[_item(title="Sign in" + bad + "again")])) == "bad_item"
        assert _refused(_file(key, items=[_item(body="Sign in" + bad + "again")])) == "bad_item"

    def test_a_line_break_is_allowed_in_the_text_only(self, key):
        assert announce.verify(_file(key, items=[_item(body="One.\nTwo.")]), now=NOW)[0][0]["body"] == "One.\nTwo."

    def test_long_running_announcement_is_refused(self, key):
        assert _refused(_file(key, items=[_item(starts=_t(0), ends=_t(31))])) == "bad_item"
        assert announce.verify(_file(key, items=[_item(starts=_t(0), ends=_t(30))]), now=NOW)

    def test_more_than_ten_is_refused(self, key):
        many = [_item(id=f"n-{i}") for i in range(11)]
        assert _refused(_file(key, items=many)) == "bad_item"
        assert len(announce.verify(_file(key, items=many[:10]), now=NOW)[0]) == 10

    def test_one_bad_announcement_refuses_the_whole_set(self, key):
        assert _refused(_file(key, items=[_item(), _item(id="second", level="loud")])) == "bad_item"
        assert _refused(_file(key, items=[_item(), _item()])) == "bad_item", "the same id twice"

    def test_unknown_fields_are_dropped(self, key):
        """There is no link, button or picture field, and an extra one never reaches the page."""
        got = announce.verify(_file(key, items=[_item(url="https://example.com", button="Update", html="<b>x</b>")]),
                              now=NOW)[0][0]
        assert set(got) == {"id", "level", "title", "body", "starts", "ends", "min_version", "max_version"}


# ── Asking for it ────────────────────────────────────────────────────────────

class TestTheFetch:
    def test_fetch_sends_no_version_or_identity(self, key):
        seen = {}

        def handler(request):
            seen.update(url=str(request.url), headers=dict(request.headers), body=request.content)
            return httpx.Response(200, content=_file(key))
        announce._http_get(httpx.MockTransport(handler))
        assert seen["url"] == announce.FEED_URL and seen["body"] == b""
        assert seen["headers"]["user-agent"] == "PawPoller"
        for name in ("cookie", "authorization", "if-none-match", "if-modified-since", "referer"):
            assert name not in seen["headers"]
        assert config.APP_VERSION not in json.dumps(seen["headers"])

    def test_fetch_refuses_redirect(self, key):
        hops = []

        def handler(request):
            hops.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://example.com/elsewhere"})
        with pytest.raises(announce.Refused) as e:
            announce._http_get(httpx.MockTransport(handler))
        assert e.value.reason == "status" and len(hops) == 1, "the redirect was not followed"

    def test_fetch_refuses_oversize(self, key):
        big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (announce.MAX_BYTES + 1)))
        with pytest.raises(announce.Refused) as e:
            announce._http_get(big)
        assert e.value.reason == "too_big"

    def test_too_many_requests_is_a_plain_failure(self, key):
        busy = httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "3600"}))
        assert announce.fetch_once(lambda: announce._http_get(busy)) == "status"
        assert announce.load() == []

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"v": 1}', b"[" * 50000,
                                     b'{"v":1,"key":"t1","payload":"!!!","sig":"!!!"}'],
                             ids=["empty", "not-json", "a-list", "no-fields", "nested-50k-deep", "bad-base64"])
    def test_garbage_and_deeply_nested_answers_are_plain_failures(self, key, raw):
        assert announce.fetch_once(lambda: raw) in ("malformed", "bad_signature")
        assert not announce._state_path().exists()

    def test_failed_check_stores_and_logs_nothing_of_the_content(self, key, caplog):
        forged = _file(Ed25519PrivateKey.generate(), items=[_item(title="SECRET-MARKER-TEXT")])
        with caplog.at_level(logging.DEBUG):
            assert announce.fetch_once(lambda: forged) == "bad_signature"
        assert not announce._state_path().exists()
        assert "SECRET-MARKER-TEXT" not in caplog.text and base64.b64encode(b"SECRET").decode()[:6] not in caplog.text
        assert [r.getMessage() for r in caplog.records if r.name == "announce"] == \
            ["announcements: check failed (bad_signature)"]

    def test_a_dead_network_is_a_plain_failure(self, key):
        def down():
            raise httpx.ConnectError("no route")
        assert announce.fetch_once(down) == "unreachable"

    def test_a_good_fetch_is_kept_and_an_older_one_after_it_is_not(self, key):
        assert announce.fetch_once(lambda: _file(key, issued=_t(-1))) == "ok"
        assert announce.fetch_once(lambda: _file(key, items=[], issued=_t(-3))) == "older"
        assert [a["id"] for a in announce.load()] == ["sample-notice"]

    def test_marker_never_goes_back(self, key):
        announce._save(_file(key, issued=_t(-1)), _t(-1))
        announce._save(_file(key, issued=_t(-5)), _t(-5))   # however it got here
        assert announce._read_state()["issued_at"] == _t(-1)

    def test_stored_file_is_verified_on_every_read(self, key):
        """A file placed by a backup, a sync or by hand is trusted no more than one from the network."""
        forged = _file(Ed25519PrivateKey.generate())
        announce._state_path().write_text(json.dumps({"file": forged.decode(), "issued_at": _t()}), encoding="utf-8")
        assert announce.load() == [] and announce.current(now=NOW) == []
        announce._state_path().write_text("{not json", encoding="utf-8")
        assert announce.load() == []

    def test_switch_off_makes_no_request(self, key):
        config.save_settings({"announcements_enabled": False})
        calls = []
        assert announce.fetch_once(lambda: calls.append(1) or _file(key)) == "off"
        assert calls == []

    def test_no_keys_means_no_request_and_no_bar(self, key, monkeypatch):
        """Until the operator's key ships, the release contacts nobody and shows nothing."""
        announce._save(_file(key), _t())
        monkeypatch.setattr(announce, "PUBLIC_KEYS", {})
        calls = []
        assert announce.fetch_once(lambda: calls.append(1) or b"{}") == "off"
        assert calls == [] and announce.load() == [] and announce.current(now=NOW) == []
        import asyncio
        asyncio.run(asyncio.wait_for(announce.loop(), timeout=1))   # returns at once: no sleep, no fetch

    def test_hosted_seat_is_always_on(self, key, monkeypatch):
        import legal
        config.save_settings({"announcements_enabled": False})
        monkeypatch.setattr(legal, "hosted", lambda: True)
        assert announce.enabled() is True
        assert announce.fetch_once(lambda: _file(key)) == "ok"

    def test_csp_is_not_widened(self):
        """The page never contacts GitHub itself: it reads announcements from its own install."""
        import dashboard
        assert "github" not in dashboard._build_csp().lower()


# ── What shows ───────────────────────────────────────────────────────────────

class TestWhatShows:
    def _hold(self, key, items):
        announce._save(_file(key, items=items), _t())

    def test_read_returns_only_current_unclosed_in_range(self, key):
        self._hold(key, [
            _item(id="now"), _item(id="later", starts=_t(1), ends=_t(3)), _item(id="over", starts=_t(-9), ends=_t(-1)),
            _item(id="old-versions", max_version="1.0.0"), _item(id="new-versions", min_version="99.0.0"),
            _item(id="this-version", min_version=config.APP_VERSION, max_version=config.APP_VERSION),
            _item(id="closed"),
        ])
        config.save_settings({"announce_closed": ["closed"]})
        assert sorted(a["id"] for a in announce.current(now=NOW)) == ["now", "this-version"]

    def test_most_serious_first(self, key):
        self._hold(key, [_item(id="b-info", level="info"), _item(id="c-urgent", level="urgent", starts=_t(-3)),
                         _item(id="a-heads-up"), _item(id="d-urgent-newer", level="urgent", starts=_t(-2))])
        assert [a["id"] for a in announce.current(now=NOW)] == ["d-urgent-newer", "c-urgent", "a-heads-up", "b-info"]

    def test_the_page_gets_words_only(self, key):
        self._hold(key, [_item()])
        assert set(announce.current(now=NOW)[0]) == {"id", "level", "title", "body"}


# ── The install's own requests ───────────────────────────────────────────────

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


class TestTheRoutes:
    def _hold(self, key, items=None):
        announce._save(_file(key, items=items, issued=datetime.now(timezone.utc).strftime(T)),
                       datetime.now(timezone.utc).strftime(T))

    def _live(self, **over):
        now = datetime.now(timezone.utc)
        return _item(starts=(now - timedelta(days=1)).strftime(T), ends=(now + timedelta(days=5)).strftime(T), **over)

    def test_read_never_fetches(self, key, client, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("a page request must never reach the network")
        monkeypatch.setattr(announce, "_http_get", boom)
        monkeypatch.setattr(announce, "fetch_once", boom)
        self._hold(key, [self._live()])
        out = client.get("/api/announcements").json()
        assert out["enabled"] is True and [a["id"] for a in out["items"]] == ["sample-notice"]

    def test_close_is_remembered(self, key, client):
        self._hold(key, [self._live(), self._live(id="second", level="info")])
        out = client.post("/api/announcements/close", json={"id": "sample-notice"}).json()
        assert [a["id"] for a in out["items"]] == ["second"]
        assert [a["id"] for a in client.get("/api/announcements").json()["items"]] == ["second"]

    def test_unknown_id_is_refused(self, key, client):
        self._hold(key, [self._live()])
        assert client.post("/api/announcements/close", json={"id": "never-published"}).status_code == 404
        assert config.get_settings().get("announce_closed") == []

    @pytest.mark.parametrize("body", [{"id": "x" * 41}, {"id": ""}, {"id": ["a"]}, {"id": 7}, {}])
    def test_over_long_request_is_refused(self, key, client, body):
        assert client.post("/api/announcements/close", json=body).status_code == 400

    def test_closed_ids_are_pruned_to_the_accepted_set(self, key, client):
        config.save_settings({"announce_closed": ["long-gone", "also-gone"]})
        self._hold(key, [self._live()])
        client.post("/api/announcements/close", json={"id": "sample-notice"})
        assert config.get_settings()["announce_closed"] == ["sample-notice"]

    def test_switch_off_hides_fetched_announcements(self, key, client):
        self._hold(key, [self._live()])
        out = client.post("/api/announcements/enabled", json={"enabled": False}).json()
        assert out == {"enabled": False, "hosted": False, "items": []}
        assert client.post("/api/announcements/enabled", json={"enabled": True}).json()["items"]

    def test_the_switch_takes_only_true_or_false(self, key, client):
        client.post("/api/announcements/enabled", json={"enabled": "no"})
        assert config.get_settings()["announcements_enabled"] is True

    def test_hosted_seat_ignores_switch_off(self, key, client, monkeypatch):
        import legal
        monkeypatch.setattr(legal, "hosted", lambda: True)
        self._hold(key, [self._live()])
        out = client.post("/api/announcements/enabled", json={"enabled": False}).json()
        assert out["enabled"] is True and out["hosted"] is True and out["items"]
        assert config.get_settings()["announcements_enabled"] is True


# ── A copy with no password, and a signed-out caller ─────────────────────────

CHANGES = [("/api/announcements/close", {"id": "sample-notice"}), ("/api/announcements/enabled", {"enabled": False}),
           ("/api/platforms/attention/close", {"all": True}), ("/api/platforms/attention/show", {})]


class TestWhoMayChangeThings:
    """TestClient's peer is "testclient", which is not loopback: it is the stranger on the network."""

    def _app(self):
        import dashboard
        return TestClient(dashboard.app)

    @pytest.mark.parametrize("path,body", CHANGES)
    def test_remote_caller_on_open_copy_cannot_close(self, key, monkeypatch, path, body):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
        assert self._app().post(path, json=body).status_code == 403

    def test_remote_caller_on_open_copy_cannot_switch_off(self, key, monkeypatch):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
        self._app().post("/api/announcements/enabled", json={"enabled": False})
        self._app().post("/api/settings/preferences", json={"announcements_enabled": False})   # not a preference
        assert config.get_settings()["announcements_enabled"] is True

    def test_remote_caller_on_open_copy_can_still_read(self, key, monkeypatch):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
        assert self._app().get("/api/announcements").status_code == 200

    def test_paired_desktop_can_close(self, key, monkeypatch):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
        monkeypatch.setattr(config, "validate_api_key", lambda k: k == "pp_paired")
        now = datetime.now(timezone.utc)
        announce._save(_file(key, items=[_item(starts=(now - timedelta(days=1)).strftime(T),
                                               ends=(now + timedelta(days=1)).strftime(T))],
                             issued=now.strftime(T)), now.strftime(T))
        r = self._app().post("/api/announcements/close", json={"id": "sample-notice"},
                             headers={"Authorization": "Bearer pp_paired"})
        assert r.status_code == 200 and r.json()["items"] == []

    @pytest.mark.parametrize("path,body", [("/api/announcements", None), *CHANGES])
    def test_read_needs_sign_in(self, key, monkeypatch, path, body):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
        c = self._app()
        r = c.get(path) if body is None else c.post(path, json=body)
        assert r.status_code == 401


# ── Dates are compared as text, so they must be written one way only ─────────

class TestDatesAreCanonical:
    """Implement review, Medium: strptime also takes unpadded fields and non-ASCII digits, which
    are valid times but sort wrongly as text."""

    ODD = ["2026-9-1T3:4:5Z", "\uff12\uff10\uff12\uff16-11-20T00:00:00Z", " 2026-11-20T00:00:00Z",
           "2026-11-20T00:00:00z", "2026-11-20 00:00:00Z"]

    @pytest.mark.parametrize("field", ["starts", "ends"])
    @pytest.mark.parametrize("odd", ODD, ids=["unpadded", "fullwidth-digits", "leading-space", "small-z", "no-T"])
    def test_non_canonical_dates_are_refused(self, key, field, odd):
        assert _refused(_file(key, items=[_item(**{field: odd})])) == "bad_item"

    @pytest.mark.parametrize("odd", ODD, ids=["unpadded", "fullwidth-digits", "leading-space", "small-z", "no-T"])
    def test_an_odd_marker_is_refused_and_does_not_lock_out_later_sets(self, key, odd):
        assert announce.fetch_once(lambda: _file(key, issued=odd)) == "bad_item"
        assert announce._read_state() == {}, "nothing of it was kept, so it can't outrank a genuine marker"
        assert announce.fetch_once(lambda: _file(key, issued=_t(-1))) == "ok"
        assert announce.fetch_once(lambda: _file(key, issued=_t())) == "ok"

    def test_a_version_cannot_be_made_expensive(self, key):
        assert _refused(_file(key, items=[_item(min_version="9" * 5000)])) == "bad_item"
        assert _refused(_file(key, items=[_item(max_version="\u0664.92.0")])) == "bad_item", "ASCII digits only"

    def test_a_lone_surrogate_is_refused(self, key):
        """It would pass as text and then break the answer to every page."""
        assert _refused(_file(key, items=[_item(body="broken \ud800 text")])) == "bad_item"


class TestVersionRanges:
    """Lean review: "up to 4.91" must include 4.91.0 and 4.91.3, not stop short of them."""

    @pytest.mark.parametrize("lo,hi,version,shows", [
        ("", "4.91", "4.91.0", True), ("", "4.91", "4.91.3", True), ("", "4.91", "4.92.0", False),
        ("4.92", "", "4.92.0", True), ("4.92", "", "4.91.9", False), ("4.91.2", "4.91.2", "4.91.2", True),
        ("4", "4", "4.99.9", True), ("5", "", "4.99.9", False), ("", "", "4.92.0", True),
        ("4.91.0.1", "", "4.91.0", False),
    ])
    def test_a_shorter_limit_covers_every_version_under_it(self, key, lo, hi, version, shows):
        announce._save(_file(key, items=[_item(min_version=lo, max_version=hi)]), _t())
        assert bool(announce.current(now=NOW, version=version)) is shows


class TestSwitchingBackOnFetchesStraightAway:
    """Gap check (US4): or "turn it on and confirm they return" waits up to an hour."""

    def test_turning_it_on_asks_once_and_only_then(self, key, client, monkeypatch):
        calls = []
        monkeypatch.setattr(announce, "fetch_soon", lambda: calls.append(1))
        client.post("/api/announcements/enabled", json={"enabled": True})      # already on
        assert calls == []
        client.post("/api/announcements/enabled", json={"enabled": False})
        assert calls == []
        client.post("/api/announcements/enabled", json={"enabled": True})
        assert calls == [1]

    def test_fetch_soon_does_nothing_under_test_or_without_keys(self, key, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no thread, no request")
        monkeypatch.setattr(announce, "fetch_once", boom)
        import threading
        before = threading.active_count()
        announce.fetch_soon()                                   # PAWPOLLER_ANNOUNCE_LOOP=0 in the suite
        monkeypatch.setenv("PAWPOLLER_ANNOUNCE_LOOP", "1")
        monkeypatch.setattr(announce, "PUBLIC_KEYS", {})
        announce.fetch_soon()
        assert threading.active_count() == before
