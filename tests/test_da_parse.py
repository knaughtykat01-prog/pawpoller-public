"""Unit tests for the DeviantArt (da) client's official-API parsing logic.

These exercise the pure helpers and DAClient._build_detail against fixture
objects shaped like real /gallery/all + /deviation/metadata responses — no
network. They lock in: the integer-id-from-URL parse, thumbnail selection,
Unix→ISO date conversion, HTML stripping, the OAuth-vs-cookie mode switch, and
the metadata→detail mapping (favourites→favorites_count, is_mature→rating,
tags→keywords, stats passthrough).
"""

from clients.da.client import (
    DAClient, _int_id_from_url, _pick_thumb, _unix_to_iso, _strip_html, _chunks,
)


# ── pure helpers ──────────────────────────────────────────────

def test_int_id_from_url():
    assert _int_id_from_url(
        "https://www.deviantart.com/knaughtykat/art/PFP-1351854174") == 1351854174
    assert _int_id_from_url("https://www.deviantart.com/u/art/Title-1/") == 1
    assert _int_id_from_url("https://www.deviantart.com/u/status/abcxyz") is None
    assert _int_id_from_url("") is None
    assert _int_id_from_url(None) is None


def test_pick_thumb_prefers_largest_then_content():
    assert _pick_thumb({"thumbs": [{"src": "small"}, {"src": "big"}]}) == "big"
    assert _pick_thumb({"thumbs": [], "content": {"src": "c"}}) == "c"
    assert _pick_thumb({"preview": {"src": "p"}}) == "p"
    assert _pick_thumb({}) == ""
    # a thumbs entry without src falls through to content
    assert _pick_thumb({"thumbs": [{"height": 1}], "content": {"src": "c"}}) == "c"


def test_unix_to_iso():
    iso = _unix_to_iso(1782919233)
    assert len(iso) == 19 and iso[4] == "-" and iso[13] == ":"
    assert _unix_to_iso("1782919233") == iso  # digit-string == int
    assert _unix_to_iso("") == ""
    assert _unix_to_iso(None) == ""
    assert _unix_to_iso("not-a-number") == "not-a-number"  # best-effort passthrough


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert _strip_html("caf&eacute; &amp; tea") == "café & tea"
    assert _strip_html("") == ""
    # unescape-first: escaped markup must not survive as a live tag
    assert _strip_html("&lt;script&gt;alert(1)&lt;/script&gt;") == "alert(1)"
    assert "<" not in _strip_html("&lt;img src=x onerror=alert(1)&gt;hi")


def test_chunks():
    assert list(_chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(_chunks([], 10)) == []


# ── mode switch ───────────────────────────────────────────────

def test_use_oauth_switch():
    assert DAClient(client_id="x", client_secret="y", target_user="u")._use_oauth is True
    assert DAClient(cookie_value="c=1", target_user="u")._use_oauth is False
    # only one of id/secret is not enough
    assert DAClient(client_id="x", target_user="u")._use_oauth is False


def test_posting_style_construction_and_cookie_alias():
    # posting constructs with cookie=... — must not raise, must set cookie_value
    c = DAClient(cookie="c=1", target_user="u")
    assert c.cookie_value == "c=1" and c._use_oauth is False


def test_update_credentials_invalidates_token_on_change():
    c = DAClient(client_id="a", client_secret="b", target_user="u")
    c._app_token = "stale"
    c._app_token_expires_at = 9e18
    c.update_credentials(client_id="a2", client_secret="b2", target_user="u")
    assert c._app_token == "" and c.client_id == "a2"


# ── _build_detail: metadata + gallery cache → legacy detail shape ──

def _client_with_cache():
    c = DAClient(client_id="x", client_secret="y", target_user="knaughtykat")
    c._gallery_cache[1351854174] = {
        "uuid": "AAAA1111-2222-3333-4444-555566667777",
        "title": "PFP",
        "url": "https://www.deviantart.com/knaughtykat/art/PFP-1351854174",
        "thumbnail_url": "https://img/thumb.jpg",
        "posted_at": "2026-07-03 03:54:33",
        "is_mature": False,
        "username": "knaughtykat",
    }
    return c


def _metadata(**over):
    base = {
        "deviationid": "AAAA1111-2222-3333-4444-555566667777",
        "title": "PFP",
        "author": {"username": "knaughtykat"},
        "description": "<p>A <b>tiger</b> portrait.</p>",
        "is_mature": False,
        "submission": {"category": "Digital Art"},
        "tags": [{"tag_name": "tiger"}, {"tag_name": "feline"}],
        "stats": {"views": 10, "favourites": 2, "comments": 1, "downloads": 3,
                  "views_today": 0, "downloads_today": 0},
    }
    base.update(over)
    return base


def test_build_detail_maps_stats_and_metadata():
    c = _client_with_cache()
    d = c._build_detail(1351854174, _metadata())
    assert d["deviation_id"] == 1351854174
    assert d["title"] == "PFP"
    assert d["username"] == "knaughtykat"
    # British "favourites" from the API maps to the DB's "favorites_count"
    assert d["views"] == 10 and d["favorites_count"] == 2
    assert d["comments_count"] == 1 and d["downloads"] == 3
    assert d["rating"] == "General"
    assert d["keywords"] == ["tiger", "feline"]
    assert d["description"] == "A tiger portrait."
    assert d["category"] == "Digital Art"
    assert d["thumbnail_url"] == "https://img/thumb.jpg"
    assert d["posted_at"] == "2026-07-03 03:54:33"
    assert d["link"].endswith("PFP-1351854174")


def test_build_detail_mature_rating_and_missing_stats():
    c = _client_with_cache()
    d = c._build_detail(1351854174, _metadata(is_mature=True, stats={}))
    assert d["rating"] == "Mature"
    # missing stats default to 0, never None
    assert d["views"] == 0 and d["favorites_count"] == 0
    assert d["comments_count"] == 0 and d["downloads"] == 0


def test_build_detail_falls_back_to_cache_when_metadata_sparse():
    c = _client_with_cache()
    # metadata with no title/author → use gallery cache values
    d = c._build_detail(1351854174, _metadata(title="", author={}))
    assert d["title"] == "PFP"
    assert d["username"] == "knaughtykat"
