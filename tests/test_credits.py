"""Credits in Settings → About (backlog CREDITS).

The operator wanted to thank a beta tester. The names live in settings, never in the
source: this repo ships as a public copy and carries no real names (CLAUDE.md), and who
helped is the operator's to say. So every install starts empty and only ever holds what
someone typed into the box.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config

APP_JS = open("frontend/js/app.js", encoding="utf-8").read()


@pytest.fixture()
def client():
    from routes.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestTheSetting:
    def test_it_starts_empty(self, client):
        config.save_settings({"credits": []})
        assert client.get("/api/settings/preferences").json()["credits"] == []

    def test_it_saves_names_and_roles(self, client):
        client.post("/api/settings/preferences", json={"credits": [
            {"name": "SecondFur", "role": "Beta tester"},
            {"name": "Inkwolf", "role": ""},
        ]})
        out = client.get("/api/settings/preferences").json()["credits"]
        assert out == [{"name": "SecondFur", "role": "Beta tester"}, {"name": "Inkwolf", "role": ""}]

    def test_a_nameless_entry_is_dropped(self, client):
        client.post("/api/settings/preferences", json={"credits": [{"name": "   ", "role": "Ghost"},
                                                          {"name": "Penwright", "role": "Art"}]})
        assert [c["name"] for c in client.get("/api/settings/preferences").json()["credits"]] == ["Penwright"]

    def test_rubbish_cannot_grow_the_settings_file(self, client):
        client.post("/api/settings/preferences", json={"credits": [
            *[{"name": f"Person {i}", "role": "x"} for i in range(150)],
            "not-a-dict", 42, None,
        ]})
        out = client.get("/api/settings/preferences").json()["credits"]
        assert len(out) <= 100
        assert all(isinstance(c, dict) and c["name"] for c in out)

    def test_long_values_are_trimmed(self, client):
        client.post("/api/settings/preferences", json={"credits": [{"name": "N" * 500, "role": "R" * 500}]})
        c = client.get("/api/settings/preferences").json()["credits"][0]
        assert len(c["name"]) == 80 and len(c["role"]) == 80


class TestThePage:
    def test_names_are_escaped_on_the_way_in(self):
        """A credit is free text, and it lands in innerHTML."""
        block = APP_JS[APP_JS.index("_drawCredits()"):APP_JS.index("async _saveCredits()")]
        assert "Utils.escapeHtml(c.name" in block and "Utils.escapeHtml(c.role" in block

    def test_no_name_is_baked_into_the_source(self):
        """The public copy must carry no real names — the list ships empty."""
        block = APP_JS[APP_JS.index("_drawCredits()"):APP_JS.index("async _saveCredits()")]
        assert "Nobody added yet" in block
        assert "this._credits = Array.isArray(prefs.credits) ? prefs.credits.slice() : []" in APP_JS
