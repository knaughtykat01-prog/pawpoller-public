"""Videos and GIFs from the owner's own posts come into the Library (spec 047, VIDEOIMPORT).

Before this, X was told not to list videos at all, Instagram kept only the cover still, and
Bluesky ignored video posts — so a video post was classed as text and never offered. These
tests pin each site's parser, the stored shape, the importer (real file + the site's poster,
plain refusals, nothing half-made left behind) and the still → video upgrade.
"""
from __future__ import annotations

import json

import httpx
import pytest

from clients.tw import gallerydl
from posting import artwork_importer, artwork_reader, video_media
from database.db import get_connection

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

_KW = {"tweet_id": 1800000000000000000, "content": "a clip", "author": {"name": "Inkwolf"},
       "date": "2026-10-18 10:00:00", "favorite_count": 1, "retweet_count": 0, "reply_count": 0,
       "quote_count": 0, "bookmark_count": 0, "view_count": 9}


def _kw(**over):
    return {**_KW, **over}


# ── X: gallery-dl dump ───────────────────────────────────────────────────────

def test_gallerydl_pairs_videos_and_gifs_with_their_previews():
    raw = json.dumps([
        [3, "https://pbs.twimg.com/media/P1.jpg", _kw(extension="jpg", type="photo")],
        [3, "https://video.twimg.com/v1.mp4", _kw(extension="mp4", type="video", bitrate=2176000,
                                                     duration=12.5, width=1280, height=720)],
        [3, "https://pbs.twimg.com/ext_tw_video_thumb/v1.jpg", _kw(extension="jpg", type="preview")],
        [3, "https://video.twimg.com/tweet_video/g1.mp4", _kw(extension="mp4", type="animated_gif")],
        [3, "https://pbs.twimg.com/tweet_video_thumb/g1.jpg", _kw(extension="jpg", type="preview")],
    ])
    [t] = gallerydl._parse_dump_json(raw, "Inkwolf")
    assert t["media_urls"] == ["https://pbs.twimg.com/media/P1.jpg"]      # previews aren't photos
    assert t["thumbnail_url"] == "https://pbs.twimg.com/media/P1.jpg"
    v, g = t["video_media"]
    assert (v["kind"], v["poster"], v["duration_s"], v["width"]) == (
        "video", "https://pbs.twimg.com/ext_tw_video_thumb/v1.jpg", 12.5, 1280)
    assert (g["kind"], g["poster"]) == ("gif", "https://pbs.twimg.com/tweet_video_thumb/g1.jpg")


def test_gallerydl_video_only_tweet_gets_the_poster_as_its_picture():
    raw = json.dumps([
        [3, "https://video.twimg.com/v1.mp4", _kw(extension="mp4", type="video")],
        [3, "https://pbs.twimg.com/thumb/v1.jpg", _kw(extension="jpg", type="preview")],
    ])
    [t] = gallerydl._parse_dump_json(raw, "Inkwolf")
    assert t["media_urls"] == []
    assert t["thumbnail_url"] == "https://pbs.twimg.com/thumb/v1.jpg"
    from routes.submissions_api import classify_kind
    assert classify_kind("tw", t["content_type"], has_image=bool(t["thumbnail_url"])) == "art"


def test_gallerydl_asks_for_videos_and_previews():
    import inspect
    src = inspect.getsource(gallerydl)
    assert "extractor.twitter.videos=true" in src and "extractor.twitter.previews=true" in src
    assert "videos=false" not in src


# ── X: GraphQL + official API shapes ────────────────────────────────────────

def test_x_graphql_picks_the_best_mp4():
    media = [{"type": "photo", "media_url_https": "https://pbs.twimg.com/p.jpg"},
             {"type": "video", "media_url_https": "https://pbs.twimg.com/v.jpg",
              "original_info": {"width": 720, "height": 1280},
              "video_info": {"duration_millis": 8000, "variants": [
                  {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/v.m3u8"},
                  {"content_type": "video/mp4", "bitrate": 832000, "url": "https://video.twimg.com/lo.mp4"},
                  {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/hi.mp4"}]}}]
    [e] = video_media.from_x_graphql(media)
    assert e == {"url": "https://video.twimg.com/hi.mp4", "poster": "https://pbs.twimg.com/v.jpg",
                 "kind": "video", "width": 720, "height": 1280, "duration_s": 8.0, "bitrate": 2176000}


def test_x_official_api_reads_variants_and_preview():
    from clients.tw import official_api
    assert "variants" in official_api._MEDIA_FIELDS
    media = [{"type": "animated_gif", "preview_image_url": "https://pbs.twimg.com/g.jpg",
              "variants": [{"content_type": "video/mp4", "bit_rate": 0,
                            "url": "https://video.twimg.com/g.mp4"}]}]
    [e] = video_media.from_x_v2(media)
    assert (e["kind"], e["url"], e["poster"]) == ("gif", "https://video.twimg.com/g.mp4",
                                                   "https://pbs.twimg.com/g.jpg")


def test_entry_without_a_poster_is_dropped():
    assert video_media.entry("https://video.twimg.com/a.mp4", "") is None
    assert video_media.entry("http://insecure/a.mp4", "https://p.jpg") is None


# ── Instagram + Bluesky ──────────────────────────────────────────────────────

def test_instagram_reel_and_carousel_video_children():
    from clients.ig.client import IgClient
    c = IgClient(access_token="t")
    reel = c._parse_post({"id": "1", "media_type": "VIDEO", "media_product_type": "REELS",
                          "media_url": "https://scontent.cdninstagram.com/r.mp4",
                          "thumbnail_url": "https://scontent.cdninstagram.com/r.jpg"}, {})
    assert reel["video_media"] == [{"url": "https://scontent.cdninstagram.com/r.mp4",
                                    "poster": "https://scontent.cdninstagram.com/r.jpg", "kind": "video"}]
    album = c._parse_post({"id": "2", "media_type": "CAROUSEL_ALBUM", "children": {"data": [
        {"media_type": "IMAGE", "media_url": "https://scontent.cdninstagram.com/a.jpg"},
        {"media_type": "VIDEO", "media_url": "https://scontent.cdninstagram.com/b.mp4",
         "thumbnail_url": "https://scontent.cdninstagram.com/b.jpg"}]}}, {})
    assert album["media_urls"] == ["https://scontent.cdninstagram.com/a.jpg"]
    assert [e["url"] for e in album["video_media"]] == ["https://scontent.cdninstagram.com/b.mp4"]
    photo = c._parse_post({"id": "3", "media_type": "IMAGE", "media_url": "https://x/p.jpg"}, {})
    assert photo["video_media"] == []


def _bsky_post(did, embed):
    return {"uri": f"at://{did}/app.bsky.feed.post/3abc", "author": {"did": did, "handle": "inkwolf.test"},
            "record": {"text": "clip", "createdAt": "2026-10-18T00:00:00Z"}, "embed": embed}


def test_bluesky_own_video_points_at_the_original_blob():
    from clients.bsky.client import BskyClient
    c = BskyClient()
    c._did, c._pds_host = "did:plc:owner", "pds.example.com"
    view = {"$type": "app.bsky.embed.video#view", "cid": "bafyvid", "thumbnail": "https://video.bsky.app/t.jpg",
            "playlist": "https://video.bsky.app/p.m3u8", "aspectRatio": {"width": 16, "height": 9}}
    d = c._parse_post(_bsky_post("did:plc:owner", view))
    [e] = d["video_media"]
    assert e["url"] == "https://pds.example.com/xrpc/com.atproto.sync.getBlob?did=did:plc:owner&cid=bafyvid"
    assert d["thumbnail_url"] == "https://video.bsky.app/t.jpg"
    # Inside a quote-with-media too.
    quoted = c._parse_post(_bsky_post("did:plc:owner", {"$type": "app.bsky.embed.recordWithMedia#view",
                                                        "media": view, "record": {}}))
    assert len(quoted["video_media"]) == 1
    # Someone else's video is never fetched.
    assert c._parse_post(_bsky_post("did:plc:someone", view))["video_media"] == []


# ── Found list ───────────────────────────────────────────────────────────────

def test_summary_kind_for_the_badge():
    assert video_media.summary_kind("") == ""
    assert video_media.summary_kind(json.dumps([{"url": "https://a", "kind": "gif"}])) == "gif"
    assert video_media.summary_kind([{"url": "https://a", "kind": "gif"},
                                     {"url": "https://b", "kind": "video"}]) == "video"


# ── Download refusals ────────────────────────────────────────────────────────

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_download_refuses_too_big_gone_and_not_a_video():
    big = _client(lambda r: httpx.Response(200, headers={"content-length": str(10 * 1024 * 1024)}, content=b""))
    with pytest.raises(ValueError, match="takes up to"):
        video_media.download(big, "https://v/a.mp4", cap=1024 * 1024)
    stream = _client(lambda r: httpx.Response(200, content=iter([MP4 * 20000, MP4 * 20000])))  # chunked: no length
    with pytest.raises(ValueError, match="over"):
        video_media.download(stream, "https://v/a.mp4", cap=1024 * 1024)
    with pytest.raises(ValueError, match="Poll again"):
        video_media.download(_client(lambda r: httpx.Response(410)), "https://v/a.mp4")
    with pytest.raises(ValueError, match="isn't a video"):
        video_media.download(_client(lambda r: httpx.Response(200, content=b"<html>")), "https://v/a.mp4")
    assert video_media.download(_client(lambda r: httpx.Response(200, content=MP4)), "https://v/a.mp4") == MP4


# ── Import end to end ───────────────────────────────────────────────────────

@pytest.fixture()
def archive(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    root.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return root


@pytest.fixture()
def web(monkeypatch):
    """Every httpx.Client the importer opens is served from `routes` (url → response)."""
    routes: dict = {}
    real = httpx.Client
    # No DNS in the suite: every site host "resolves" to a public address; the guard still runs.
    monkeypatch.setattr(video_media, "_resolve",
                        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))])

    def handler(req):
        return routes.get(str(req.url)) or httpx.Response(404)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return routes


def _store_tweet(tid, video_media_list, media_urls=(), thumb=""):
    from database import tw_queries
    conn = get_connection()
    try:
        tw_queries.upsert_tw_submission(conn, {
            "tweet_id": tid, "title": "Sample clip", "username": "Inkwolf", "content_type": "tweet",
            "link": f"https://x.com/Inkwolf/status/{tid}", "thumbnail_url": thumb,
            "media_urls": list(media_urls), "video_media": video_media_list}, account_id=1)
        conn.commit()
    finally:
        conn.close()


def test_video_post_imports_the_real_file_with_the_site_poster(archive, web):
    web["https://video.twimg.com/g.mp4"] = httpx.Response(200, content=MP4)
    web["https://pbs.twimg.com/g.jpg"] = httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    _store_tweet("1801", [{"url": "https://video.twimg.com/g.mp4", "poster": "https://pbs.twimg.com/g.jpg",
                           "kind": "gif", "width": 480, "height": 270}], thumb="https://pbs.twimg.com/g.jpg")
    res = artwork_importer.import_artwork("tw", "1801")
    assert res["status"] == "imported" and res["images"] == 1     # one piece, not a still + a video
    meta = artwork_reader.read_raw_metadata(res["name"])
    assert meta["image"] == "video.mp4" and meta["thumbnail"] == "poster.jpg"
    assert meta["media"]["loop"] is True and meta["media"]["width"] == 480
    assert meta["import_source"]["video_index"] == 0
    assert artwork_importer.import_artwork("tw", "1801")["status"] == "already_imported"


def test_a_failed_video_leaves_nothing_and_says_why(archive, web):
    web["https://pbs.twimg.com/g.jpg"] = httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    web["https://video.twimg.com/g.mp4"] = httpx.Response(410)
    _store_tweet("1802", [{"url": "https://video.twimg.com/g.mp4", "poster": "https://pbs.twimg.com/g.jpg",
                           "kind": "video"}], thumb="https://pbs.twimg.com/g.jpg")
    with pytest.raises(ValueError, match="Poll again"):
        artwork_importer.import_artwork("tw", "1802")
    assert list(archive.iterdir()) == []


def test_mixed_post_numbers_pieces_across_photos_and_videos(archive, web):
    web["https://pbs.twimg.com/p.jpg"] = httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    web["https://pbs.twimg.com/v.jpg"] = httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    web["https://video.twimg.com/v.mp4"] = httpx.Response(200, content=MP4)
    _store_tweet("1803", [{"url": "https://video.twimg.com/v.mp4", "poster": "https://pbs.twimg.com/v.jpg",
                           "kind": "video"}], media_urls=["https://pbs.twimg.com/p.jpg"],
                 thumb="https://pbs.twimg.com/p.jpg")
    res = artwork_importer.import_artwork("tw", "1803")
    titles = sorted(artwork_reader.read_raw_metadata(n)["title"] for n in res["names"])
    assert titles == ["Sample clip (1/2)", "Sample clip (2/2)"]


def test_a_still_takes_its_video_and_keeps_the_picture(archive, web):
    web["https://pbs.twimg.com/v.jpg"] = httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    web["https://video.twimg.com/v.mp4"] = httpx.Response(200, content=MP4)
    # An earlier import brought in only the cover still.
    _store_tweet("1804", [], thumb="https://pbs.twimg.com/v.jpg")
    name = artwork_importer.import_artwork("tw", "1804")["name"]
    old_image = artwork_reader.read_raw_metadata(name)["image"]
    # A later poll learns about the video.
    _store_tweet("1804", [{"url": "https://video.twimg.com/v.mp4", "poster": "https://pbs.twimg.com/v.jpg",
                           "kind": "video"}], thumb="https://pbs.twimg.com/v.jpg")
    assert artwork_importer.import_artwork("tw", "1804").get("video_available") is True
    assert artwork_importer.attach_source_video("tw", "1804") == {"status": "attached", "name": name}
    meta = artwork_reader.read_raw_metadata(name)
    assert meta["image"] == "video.mp4" and meta["thumbnail"] == old_image
    assert (archive / name / old_image).is_file()                  # nothing deleted
    with pytest.raises(artwork_importer.AlreadyVideo):
        artwork_importer.attach_source_video("tw", "1804")


# ── Migration ────────────────────────────────────────────────────────────────

def test_migration_adds_the_column_to_an_older_database():
    from database.db import _run_migrations
    conn = get_connection()
    try:
        for t in ("tw_submissions", "ig_submissions", "bsky_submissions"):
            conn.execute(f"ALTER TABLE {t} DROP COLUMN video_media")
        _run_migrations(conn)
        for t in ("tw_submissions", "ig_submissions", "bsky_submissions"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            assert "video_media" in cols, t
    finally:
        conn.close()


# ── Only the public internet (security review, 4.82.0) ──────────────────────

def _guarded(monkeypatch, handler, addr_for):
    monkeypatch.setattr(video_media, "_resolve",
                        lambda host, port, **kw: [(2, 1, 6, "", (addr_for(host), port))])
    c = video_media.public_client()
    c._transport = httpx.MockTransport(handler)
    return c


def test_a_redirect_to_an_internal_address_is_refused(monkeypatch):
    def handler(req):
        if req.url.host == "cdn.example.com":
            return httpx.Response(302, headers={"location": "http://router.lan/snapshot.jpg"})
        return httpx.Response(200, content=PNG)
    addrs = {"cdn.example.com": "93.184.216.34", "router.lan": "192.168.1.1"}
    with _guarded(monkeypatch, handler, addrs.get) as c:
        with pytest.raises(video_media.NotPublic, match="public internet"):
            artwork_importer._poster(c, "https://cdn.example.com/p.jpg")


def test_internal_hosts_and_odd_schemes_are_refused(monkeypatch):
    with _guarded(monkeypatch, lambda r: httpx.Response(200, content=MP4), lambda h: h) as c:
        for url in ("https://127.0.0.1/a.mp4", "https://169.254.169.254/latest", "https://10.0.0.5/a.mp4"):
            with pytest.raises(video_media.NotPublic):
                video_media.download(c, url)
    with pytest.raises(video_media.NotPublic):
        video_media._guard(httpx.Request("GET", "ftp://example.com/a.mp4"))


def test_errors_never_repeat_the_signed_link():
    c = _client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError) as e:
        video_media.download(c, "https://scontent.cdninstagram.com/v.mp4?oh=SECRET&oe=1")
    assert "SECRET" not in str(e.value) and "500" in str(e.value)


def test_poster_is_capped():
    c = _client(lambda r: httpx.Response(200, headers={"content-length": str(video_media.POSTER_CAP + 1)}))
    with pytest.raises(ValueError, match="picture"):
        artwork_importer._poster(c, "https://pbs.twimg.com/p.jpg")
