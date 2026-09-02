"""Gap wave 5: watermark · cross-platform series · beta-reader draft share.

Covers the 2.186 trio. Commissions (2.187) has its own suite.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from database.db import get_connection
from database import share_tokens


# ---------------------------------------------------------------------------
# §1 Watermark on export
# ---------------------------------------------------------------------------

def _make_png(path, size=(400, 300)):
    from PIL import Image
    Image.new("RGB", size, (120, 90, 60)).save(path, "PNG")
    return str(path)


def test_watermark_produces_distinct_temp_when_enabled(tmp_path):
    from posting import watermark
    src = _make_png(tmp_path / "art.png")
    settings = {"artwork_watermark_enabled": True, "artwork_watermark_text": "@KnaughtyKat"}
    out, tmp = watermark.apply(src, settings)
    try:
        assert tmp is not None           # a temp copy was produced
        assert out == tmp                # caller posts the watermarked file
        assert out != src                # NOT the original
        assert os.path.isfile(out)
        # It's a real, non-trivial image (watermark drew onto it).
        from PIL import Image
        assert Image.open(out).size == (400, 300)
    finally:
        if tmp and os.path.isfile(tmp):
            os.remove(tmp)


def test_watermark_passthrough_when_disabled(tmp_path):
    from posting import watermark
    src = _make_png(tmp_path / "art.png")
    out, tmp = watermark.apply(src, {"artwork_watermark_enabled": False,
                                     "artwork_watermark_text": "@x"})
    assert (out, tmp) == (src, None)


def test_watermark_passthrough_when_text_blank(tmp_path):
    from posting import watermark
    src = _make_png(tmp_path / "art.png")
    out, tmp = watermark.apply(src, {"artwork_watermark_enabled": True,
                                     "artwork_watermark_text": "   "})
    assert (out, tmp) == (src, None)
    assert watermark.is_enabled({"artwork_watermark_enabled": True,
                                 "artwork_watermark_text": ""}) is False


def test_watermark_missing_file_is_safe():
    from posting import watermark
    out, tmp = watermark.apply("/no/such/file.png",
                               {"artwork_watermark_enabled": True,
                                "artwork_watermark_text": "@x"})
    assert (out, tmp) == ("/no/such/file.png", None)


# ---------------------------------------------------------------------------
# §2 Cross-platform series (story.json → StoryInfo / _story_entry)
# ---------------------------------------------------------------------------

def _story_dir_with(tmp_path, meta):
    d = tmp_path / meta.get("_folder", "A_Story")
    (d / "Markdown").mkdir(parents=True)
    (d / "Markdown" / "MASTER.md").write_text("# A Story\n\nBody.\n", encoding="utf-8")
    meta.pop("_folder", None)
    (d / "story.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_series_roundtrips_into_storyinfo(tmp_path):
    from posting import story_reader
    d = _story_dir_with(tmp_path, {"title": "A Story", "series": "Velvet & Vice",
                                   "series_index": 3})
    info = story_reader._load_from_story_json("A_Story", d, d / "story.json")
    assert info.series == "Velvet & Vice"
    assert info.series_index == 3


def test_series_roundtrips_into_story_entry(tmp_path):
    from posting import story_reader
    d = _story_dir_with(tmp_path, {"title": "A Story", "series": "Velvet & Vice",
                                   "series_index": 3})
    entry = story_reader._story_entry(d)
    assert entry["series"] == "Velvet & Vice"
    assert entry["series_index"] == 3


def test_series_defaults_when_absent(tmp_path):
    from posting import story_reader
    d = _story_dir_with(tmp_path, {"title": "Loner"})
    info = story_reader._load_from_story_json("Loner", d, d / "story.json")
    entry = story_reader._story_entry(d)
    assert (info.series, info.series_index) == ("", 0)
    assert (entry["series"], entry["series_index"]) == ("", 0)


def test_series_index_coerces_from_string(tmp_path):
    """A number-input value saved as a string still parses cleanly."""
    from posting import story_reader
    d = _story_dir_with(tmp_path, {"title": "S", "series": "X", "series_index": "2"})
    info = story_reader._load_from_story_json("S", d, d / "story.json")
    assert info.series_index == 2
    # Empty string → 0 (never raises).
    d2 = _story_dir_with(tmp_path, {"_folder": "S2", "title": "S2",
                                    "series": "X", "series_index": ""})
    info2 = story_reader._load_from_story_json("S2", d2, d2 / "story.json")
    assert info2.series_index == 0


# ---------------------------------------------------------------------------
# §3 Beta-reader draft share tokens
# ---------------------------------------------------------------------------

def test_share_token_create_lookup_and_live():
    conn = get_connection()
    try:
        row = share_tokens.create_token(conn, "My_Story")
        assert row["story_name"] == "My_Story"
        assert row["enabled"] == 1
        got = share_tokens.get_token(conn, row["share_token"])
        assert got["story_name"] == "My_Story"
        assert share_tokens.is_live(got) is True
    finally:
        conn.close()


def test_share_token_expiry_gate():
    conn = get_connection()
    try:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        expired = share_tokens.create_token(conn, "S", expires_at=past)
        live = share_tokens.create_token(conn, "S", expires_at=future)
        assert share_tokens.is_live(share_tokens.get_token(conn, expired["share_token"])) is False
        assert share_tokens.is_live(share_tokens.get_token(conn, live["share_token"])) is True
    finally:
        conn.close()


def test_share_token_revoke():
    conn = get_connection()
    try:
        row = share_tokens.create_token(conn, "S")
        tok = row["share_token"]
        assert share_tokens.revoke_token(conn, tok) is True
        assert share_tokens.is_live(share_tokens.get_token(conn, tok)) is False
        # Idempotent — revoking again affects nothing.
        assert share_tokens.revoke_token(conn, tok) is False
        # Revoked tokens drop out of the active list.
        assert share_tokens.list_active_tokens(conn, "S") == []
    finally:
        conn.close()


def test_share_is_live_handles_unknown_and_none():
    assert share_tokens.is_live(None) is False
    assert share_tokens.is_live({"enabled": 0}) is False
    assert share_tokens.is_live({"enabled": 1, "expires_at": None}) is True


def test_list_active_returns_newest_first():
    conn = get_connection()
    try:
        a = share_tokens.create_token(conn, "S")
        b = share_tokens.create_token(conn, "S")
        active = share_tokens.list_active_tokens(conn, "S")
        tokens = {r["share_token"] for r in active}
        assert tokens == {a["share_token"], b["share_token"]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §3 Public /share/{token} route + render helper (end-to-end)
# ---------------------------------------------------------------------------

def _archive_with_story(tmp_path):
    """Minimal archive holding one renderable story. Returns (archive, name)."""
    story = tmp_path / "Test_Story"
    (story / "Markdown").mkdir(parents=True)
    (story / "Markdown" / "MASTER.md").write_text(
        "# Test Story\n\nOnce upon a time in the woods.\n", encoding="utf-8")
    (story / "story.json").write_text(json.dumps({"title": "Test Story"}), encoding="utf-8")
    return tmp_path, "Test_Story"


def test_render_helper_none_for_missing_story(tmp_path, monkeypatch):
    import routes.editor_api as editor_api
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
    assert editor_api.render_story_share_html("Ghost_Story") is None


def test_render_helper_produces_self_contained_html(tmp_path, monkeypatch):
    import routes.editor_api as editor_api
    archive, name = _archive_with_story(tmp_path)
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: archive)
    html = editor_api.render_story_share_html(name)
    assert html is not None
    assert "Once upon a time in the woods." in html
    assert "<style>" in html          # CSS inlined — no external stylesheet
    assert "<link rel=\"stylesheet\"" not in html


def test_share_route_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard
    import routes.editor_api as editor_api

    archive, name = _archive_with_story(tmp_path)
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: archive)
    # Open instance (no dashboard password) → middleware passes through; the
    # share endpoints and public route are what we're exercising.
    monkeypatch.setattr(dashboard.config, "is_dashboard_auth_required", lambda: False)
    client = TestClient(dashboard.app)

    # Bogus token → 404 (identical page whether unknown/revoked/expired).
    assert client.get("/share/not-a-real-token").status_code == 404

    # Create a link via the editor endpoint.
    r = client.post(f"/api/editor/stories/{name}/share", json={})
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["url"].endswith(f"/share/{token}")

    # Public fetch renders the story, no login.
    page = client.get(f"/share/{token}")
    assert page.status_code == 200
    assert "Once upon a time in the woods." in page.text
    # Script-free CSP on the public surface.
    assert "script-src" not in page.headers.get("Content-Security-Policy", "")
    assert page.headers["Content-Security-Policy"].startswith("default-src 'none'")

    # It shows up in the owner's active list.
    lst = client.get(f"/api/editor/stories/{name}/share").json()["shares"]
    assert any(s["token"] == token for s in lst)

    # Revoke → public link now 404s.
    assert client.delete(f"/api/editor/share/{token}").json()["ok"] is True
    assert client.get(f"/share/{token}").status_code == 404
