"""The 18+ step and the under-18 lock (LEGALPAGES, 4.58.0).

Decided 2026-10-04: under-18s may use PawPoller for safe-for-work work; adult features lock. The lock
is enforced on the server, at the same gates that already refuse what a site doesn't take, so no page
or route can go round it.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

import age_gate
import config
from database.accounts import PLATFORM_NAMES


def _under18():
    config.save_settings({"age_band": "under18"})


def test_every_platform_has_its_own_minimum_age():
    assert set(age_gate.SITE_AGES) == set(PLATFORM_NAMES)
    assert {c for c, v in age_gate.SITE_AGES.items() if v["adult_only"]} == {"fa", "ib", "e621", "r34"}


def test_adults_and_unanswered_installs_are_not_locked():
    for band in ("", "adult"):
        config.save_settings({"age_band": band})
        assert age_gate.refusal("e621", "adult") is None


def test_under_18_locks_adult_sites_and_ratings():
    _under18()
    assert "adults only" in age_gate.refusal("e621", "general", site_name="e621")
    assert "locked for under-18s" in age_gate.refusal("bsky", "mature")
    assert age_gate.refusal("bsky", "general") is None


def test_the_posting_gate_refuses_before_the_network():
    from posting.platforms.base import StoryUploadPackage
    from posting.manager import _POSTER_CLASSES
    import importlib
    _under18()
    mod, cls = _POSTER_CLASSES["ws"]
    poster = getattr(importlib.import_module(mod), cls)()
    pkg = StoryUploadPackage(story_name="x", chapter_index=0, chapter_title="", platform="ws",
                             title="x", description="", rating="adult")
    assert "locked for under-18s" in poster.refusal(pkg)


def test_a_post_is_refused_and_the_preview_says_so():
    from posting import post_publisher
    _under18()
    r = asyncio.run(post_publisher._publish_one({"body": "hi", "rating": "mature"}, "bsky", None, None))
    assert r["success"] is False and "under-18" in r["error"]
    sites = post_publisher.preview("hi", ["bsky"], rating="mature")
    assert any("under-18" in w["text"] for w in sites["bsky"]["warnings"]), sites


def test_the_pickers_grey_adult_sites_and_ratings():
    from routes import api as api_routes
    _under18()
    out = api_routes.platforms_media()["platforms"]
    assert "adults only" in out["e621"]["blocked"]
    assert out["bsky"]["blocked"] == "" and out["bsky"]["max_rating"] == "general"
    assert "under-18" in out["bsky"]["rating_refusals"]["adult"]


def test_the_age_route_answers_and_saves():
    from routes import api as api_routes
    app = FastAPI()
    app.include_router(api_routes.router)
    c = TestClient(app)
    config.save_settings({"age_band": ""})
    assert c.get("/api/age").json()["asked"] is False
    assert c.post("/api/age", json={"band": "maybe"}).status_code == 400
    r = c.post("/api/age", json={"band": "under18"}).json()
    assert r["band"] == "under18" and r["asked"] is True and len(r["sites"]) == len(PLATFORM_NAMES)
