"""Spec 054, the quick-fix bundle: one check per fix, each fails if its fix is undone.

No network, no personal data. Contracts: ``specs/054-quick-fix-bundle/contracts/http.md``.
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import config

REPO = Path(__file__).resolve().parent.parent


def _png(w=32, h=32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def _app(*routers) -> TestClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


# ── US1: nothing reads as "AI" (PROMOICON) ───────────────────────────────────

_AI_GLYPHS = re.compile(r"✨|🪄|&#10024;|&#129668;|&#x2728;|&#x1fa84;|\\u2728|\\u\{1fa84\}|\\2728|\\1fa84", re.I)


def test_no_sparkles_or_wand_glyph_in_the_frontend_or_site():
    try:
        files = subprocess.run(["git", "ls-files", "frontend", "site"], cwd=REPO, capture_output=True,
                               text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs the git checkout")
    hits = []
    for rel in files:
        if "/vendor/" in rel or not rel.endswith((".js", ".html", ".css", ".astro", ".md", ".json", ".mjs")):
            continue
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        hits += [f"{rel}:{i}" for i, line in enumerate(text.splitlines(), 1) if _AI_GLYPHS.search(line)]
    assert not hits, f"a sparkles/wand glyph reads as 'AI': {hits}"


def test_promo_maker_wears_the_megaphone():
    assert '<span class="nav-icon">&#128227;</span><span class="nav-label">Promo Maker</span>' in \
        (REPO / "frontend" / "index.html").read_text(encoding="utf-8")


# ── US3: avatar upload (SEC485) ──────────────────────────────────────────────

@pytest.fixture
def xp_api():
    from routes.xp_api import xp_router
    return _app(xp_router)


def test_avatar_happy_path_still_works(xp_api):
    r = xp_api.put("/api/xp/avatar", files={"image": ("a.png", _png(), "image/png")},
                   data={"source": "work", "work_name": "Sample Story"})
    assert r.status_code == 200, r.text
    assert xp_api.get("/api/xp/avatar").content[:4] == b"RIFF"


def test_avatar_over_the_cap_is_refused_from_its_declared_size(xp_api):
    r = xp_api.put("/api/xp/avatar", content=b"x" * 10, headers={"content-length": str(5 * 1024 * 1024),
                                                                   "content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413 and "2 MB" in r.json()["detail"]


def test_avatar_part_over_the_cap_is_refused(xp_api, monkeypatch):
    import routes.xp_api as x
    monkeypatch.setattr(x, "_AVATAR_MAX_BYTES", 100)
    r = xp_api.put("/api/xp/avatar", files={"image": ("a.png", _png(64, 64), "image/png")})
    assert r.status_code == 413


def test_avatar_needs_a_readable_length(xp_api):
    def body():
        yield b"--b\r\n"
    r = xp_api.put("/api/xp/avatar", content=body(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 411
    r = xp_api.put("/api/xp/avatar", content=b"x", headers={"content-length": "abc",
                                                            "content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 400


def test_avatar_work_name_is_capped(xp_api):
    r = xp_api.put("/api/xp/avatar", files={"image": ("a.png", _png(), "image/png")},
                   data={"source": "work", "work_name": "w" * 513})
    assert r.status_code == 400 and "too long" in r.json()["detail"]


# ── US3: announce junk → 400 (SEC485) ────────────────────────────────────────

@pytest.mark.parametrize("body", [
    {"works": [{"type": "story", "name": "Sample Story", "level": "lots"}]},
    {"works": "not a list"},
    {"medals": ["not an object"]},
])
def test_a_malformed_announcement_is_a_400(body):
    from routes.progress_api import progress_router
    r = _app(progress_router).post("/api/progress/announce", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "Malformed announcement"


def test_the_medal_icon_is_escaped():
    assert "${esc(m.icon || '🏅')}" in (REPO / "frontend" / "js" / "progress.js").read_text(encoding="utf-8")


# ── US3: Twitch webhook (REVIEW4811) ─────────────────────────────────────────

@pytest.fixture
def twitch(monkeypatch):
    import routes.ttv_api as r
    from polling import golive
    monkeypatch.setattr(golive, "webhook_base", lambda: "https://pub.example")
    monkeypatch.setattr(golive, "eventsub_secret", lambda: "s3cret")
    monkeypatch.setattr(r, "_last_refusal_log", 0.0)
    return _app(r.ttv_hooks_router)


def test_twitch_refuses_a_non_numeric_length(twitch):
    assert twitch.post("/hooks/twitch", content=b"{}", headers={"content-length": "abc"}).status_code == 400


def test_twitch_stops_reading_an_oversized_chunked_body(twitch):
    def body():
        for _ in range(20):
            yield b"x" * 8192          # 160 KB, no Content-Length
    assert twitch.post("/hooks/twitch", content=body()).status_code == 413


def test_twitch_bad_signatures_log_once_a_minute(twitch, caplog):
    h = {"Twitch-Eventsub-Message-Id": "m", "Twitch-Eventsub-Message-Timestamp": "2026-10-23T00:00:00Z",
         "Twitch-Eventsub-Message-Signature": "sha256=bad", "Twitch-Eventsub-Message-Type": "notification"}
    with caplog.at_level(logging.WARNING, logger="routes.ttv_api"):
        for _ in range(5):
            assert twitch.post("/hooks/twitch", content=b"{}", headers=h).status_code == 403
    assert sum("bad signature" in r.message for r in caplog.records) == 1


# ── US4: /app-reset (REVIEW4811) ─────────────────────────────────────────────

@pytest.mark.parametrize("site,referer,clears", [
    ("cross-site", None, False), ("same-site", None, False), ("same-origin", None, True), ("none", None, True),
    # a browser without Sec-Fetch-Site: only a referrer from this address counts (the ask page's own link)
    (None, None, False), (None, "https://elsewhere.example/page", False), (None, "http://testserver/app-reset", True),
])
def test_app_reset_only_clears_from_this_site_or_a_typed_address(site, referer, clears):
    import dashboard
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    h = {k: v for k, v in (("Sec-Fetch-Site", site), ("Referer", referer)) if v}
    r = c.get("/app-reset", headers=h)
    assert r.status_code == 200
    assert ("clear-site-data" in r.headers) is clears
    if not clears:
        assert "href='/app-reset'" in r.text and "Nothing has been cleared" in r.text


# ── US5: the open relay (RELAYHARDEN) ────────────────────────────────────────

@pytest.fixture
def relay(monkeypatch, tmp_path):
    import consent_records
    from routes import ig_api
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    ig_api._RELAY_HITS.clear()
    state = {"ig_relay_open": True, "ig_public_base_url": "https://pub.example",
             "consent_records": {"ig_relay": {"value": True, "at": "2026-10-23T00:00:00+00:00",
                                              "wording": consent_records.WORDING["ig_relay"]}}}
    monkeypatch.setattr(config, "get_settings", lambda: dict(state))
    return _app(ig_api.ig_router)


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def test_the_open_relay_refuses_video(relay, tmp_path):
    r = relay.post("/api/ig/relay", files={"file": ("clip.mp4", MP4, "application/octet-stream")})
    assert r.status_code == 415 and "pictures only" in r.json()["detail"]
    assert not list((tmp_path / "ig_pending").glob("*.mp4"))


def test_the_open_relay_caps_pending_bytes(relay, monkeypatch):
    monkeypatch.setattr(config, "IG_RELAY_MAX_PENDING_BYTES", 1)
    assert relay.post("/api/ig/relay", files={"file": ("a.png", _png(), "image/png")}).status_code == 200
    r = relay.post("/api/ig/relay", files={"file": ("a.png", _png(), "image/png")})
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_the_open_relay_refuses_huge_images_before_decoding(relay, monkeypatch):
    monkeypatch.setattr(config, "IG_RELAY_MAX_PIXELS", 100)
    r = relay.post("/api/ig/relay", files={"file": ("a.png", _png(64, 64), "image/png")})
    assert r.status_code == 413 and "megapixels" in r.json()["detail"]


def test_the_open_relay_checks_the_length_before_the_body(relay):
    r = relay.post("/api/ig/relay", content=b"x", headers={"content-length": str(50 * 1024 * 1024),
                                                           "content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413

    def body():
        yield b"--b\r\n"
    r = relay.post("/api/ig/relay", content=body(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 411


def test_both_length_and_chunked_is_refused(relay):
    r = relay.post("/api/ig/relay", content=b"x", headers={"content-length": "1", "transfer-encoding": "chunked",
                                                           "content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 400


def test_a_desktop_reel_skips_the_relay(monkeypatch, tmp_path):
    import asyncio
    from posting import ig_host
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(MP4)
    sent = []

    async def fake_upload(endpoint, path, api_key="", http=None):
        sent.append(endpoint)
        raise RuntimeError("no tunnel in tests")
    monkeypatch.setattr(ig_host, "upload_to_host", fake_upload)
    monkeypatch.setattr(ig_host, "relay_on", lambda s=None: True)
    monkeypatch.setattr(config, "get_settings", lambda: {"ig_relay_enabled": True})
    try:
        asyncio.run(ig_host.host_images([str(clip)], {"ig_relay_enabled": True}))
    except Exception as e:
        assert "pictures only" in str(e)
    assert not any("relay" in e for e in sent)


def test_the_paired_route_still_takes_video(relay):
    """Only the open relay lost video; the signed-in /pubmedia route (paired desktops) keeps it."""
    r = relay.post("/api/ig/pubmedia", files={"file": ("clip.mp4", MP4, "application/octet-stream")})
    assert r.status_code == 200 and r.json()["url"].endswith(".mp4")


# ── US7: service worker (SW206) ──────────────────────────────────────────────

def test_the_service_worker_never_caches_a_partial_response():
    sw = (REPO / "frontend" / "sw.js").read_text(encoding="utf-8")
    assert "net.status === 200 && net.type === 'basic') cache.put(req, net.clone()).catch(" in sw
    assert "cache.put(req, net.clone());" not in sw


# ── US6: DeviantArt cookie guide (DACOOKIEGUIDE) ─────────────────────────────

def test_the_da_cookie_guide_is_numbered_steps():
    src = (REPO / "frontend" / "js" / "platform_guides.js").read_text(encoding="utf-8")
    da = src[src.index("    da: {"):src.index("    wp: {")]
    steps = re.findall(r"\{ t: 'Descriptions only: ", da)
    assert len(steps) >= 6
    for fact in ("same account PawPoller posts as", "<b>whole</b> value", "Treat it like a password",
                 "DA-COOKIE-EXPIRED"):
        assert fact in da, fact
