"""Filter the Library by platform, and hide the platforms you don't use (backlog PLATFILTER).

Asked for by an artist who posts to a handful of sites and had to read twenty everywhere.
Two halves: a platform dropdown on the Library, and one app-wide list that the Platforms
hub, the Overview charts and that dropdown all read.

Stored as the HIDDEN codes, never the shown ones — the doctrine already written into
`platforms.js::visiblePlatforms`: a platform connected later then appears by default, so
a filtered view can never quietly under-count new work.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from database import platform_metrics

PLATFORMS_JS = open("frontend/js/platforms.js", encoding="utf-8").read()
SHELF_JS = open("frontend/js/bookshelf.js", encoding="utf-8").read()
APP_JS = open("frontend/js/app.js", encoding="utf-8").read()


@pytest.fixture()
def client():
    from routes.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestTheSetting:
    def test_it_starts_empty(self, client):
        config.save_settings({"hidden_platforms": []})
        assert client.get("/api/settings/preferences").json()["hidden_platforms"] == []

    def test_it_keeps_real_platform_codes(self, client):
        keep = list(platform_metrics.ALL_CODES)[:3]
        client.post("/api/settings/preferences", json={"hidden_platforms": keep})
        assert client.get("/api/settings/preferences").json()["hidden_platforms"] == keep

    def test_it_drops_codes_that_are_not_platforms(self, client):
        good = platform_metrics.ALL_CODES[0]
        client.post("/api/settings/preferences",
                    json={"hidden_platforms": [good, "notaplatform", "", "  ", 7]})
        assert client.get("/api/settings/preferences").json()["hidden_platforms"] == [good]

    def test_it_does_not_store_the_same_code_twice(self, client):
        good = platform_metrics.ALL_CODES[0]
        client.post("/api/settings/preferences", json={"hidden_platforms": [good, good, good]})
        assert client.get("/api/settings/preferences").json()["hidden_platforms"] == [good]


class TestOneSourceOfTruth:
    def test_every_platform_list_reads_it(self):
        assert "window.HIDDEN_PLATFORMS" in PLATFORMS_JS
        block = PLATFORMS_JS[PLATFORMS_JS.index("function visiblePlatforms"):][:700]
        assert "window.HIDDEN_PLATFORMS" in block, "the shared list must honour it"
        single = PLATFORMS_JS[PLATFORMS_JS.index("function isPlatformVisible"):][:400]
        assert "window.HIDDEN_PLATFORMS" in single, "so must the per-code test"

    def test_it_is_loaded_before_anything_renders(self):
        assert "window.HIDDEN_PLATFORMS = Array.isArray(prefs && prefs.hidden_platforms)" in APP_JS

    def test_settings_offers_every_platform_to_untick(self):
        assert 'id="platform-visibility"' in APP_JS
        assert "savePreferences({ hidden_platforms: next })" in APP_JS
        wiring = APP_JS[APP_JS.index("const pvBox = document.getElementById('platform-visibility')"):][:1400]
        assert ".filter(b => !b.checked)" in wiring, "hidden = unticked, not the other way round"


class TestTheLibraryFilter:
    def test_the_dropdown_lists_only_platforms_in_use(self):
        assert 'id="shelf-platform"' in SHELF_JS
        block = SHELF_JS[SHELF_JS.index('id="shelf-platform"'):][:700]
        assert "window.visiblePlatforms" in block

    def test_it_filters_on_where_a_work_is_live(self):
        block = SHELF_JS[SHELF_JS.index("if (this._platform)"):][:300]
        assert "(w.platforms || []).includes(this._platform)" in block

    def test_every_platform_remains_the_default(self):
        assert "_platform: ''," in SHELF_JS
        assert '<option value="">Every platform</option>' in SHELF_JS
