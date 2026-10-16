"""ARTSYNCPICK (4.77.0): sync one artwork to one chosen site, like a story's drifted rows.

The page's per-site Sync button sends ``platforms: [code]``; the server must pass that
through so only that site is rewritten — every other live upload stays untouched.
"""
from fastapi.testclient import TestClient


def test_sync_passes_the_chosen_site_through(monkeypatch):
    import dashboard
    from posting import manager
    from routes import masterpieces_api

    seen = {}

    async def fake_update(name, platforms=None):
        seen["platforms"] = platforms
        return [{"platform": p, "success": True} for p in platforms or ["fa", "ws"]]

    monkeypatch.setattr(manager, "update_artwork", fake_update)
    monkeypatch.setattr(masterpieces_api.artwork_reader, "load_artwork", lambda name: {"title": "Sample"})
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    r = c.post("/api/masterpieces/Sample/sync", json={"confirm_live": True, "platforms": ["da"]})
    assert r.status_code == 200 and r.json()["synced"] == 1
    assert seen["platforms"] == ["da"]


def test_the_page_sends_only_the_rows_site():
    src = open("frontend/js/masterpieces.js", encoding="utf-8").read()
    assert "platforms: [only]" in src and 'data-mp-sync data-platform=' in src
    assert "title differs" in src


def test_a_successful_syncs_note_is_shown():
    # 4.77.2: a half-done sync (DA description without the cookie) read as fully done.
    src = open("frontend/js/components.js", encoding="utf-8").read()
    assert "r.attention || r.note" in src


def test_platforms_must_be_a_list(monkeypatch):
    """REVIEW4770: a string made `plat not in platforms` a substring match."""
    import dashboard
    from routes import masterpieces_api
    monkeypatch.setattr(masterpieces_api.artwork_reader, "load_artwork", lambda name: {"title": "Sample"})
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    r = c.post("/api/masterpieces/Sample/sync", json={"confirm_live": True, "platforms": "fa,da"})
    assert r.status_code == 400
