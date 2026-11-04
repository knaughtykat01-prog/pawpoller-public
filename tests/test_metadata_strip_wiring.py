"""Spec 071 (EXIFSTRIP): every way a picture leaves PawPoller goes out cleaned.

Stand-in posters capture the bytes they were handed; nothing touches the network. Outputs are judged by
`independent_kinds` (Pillow + raw byte searches), never by the cleaner's own parser.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import tempfile
from pathlib import Path

import pytest

import config
from database.db import get_connection, init_db
from posting import activity, artwork_reader, manager, metadata_strip as ms
from posting.platforms.base import PostResult
from tests import _metadata_fixtures as fx

ROOT = Path(__file__).resolve().parent.parent
OK = {"success": True, "external_id": "1", "external_url": "https://example.com/1"}


# ── Harness ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    root = tmp_path / "systemp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def settings(monkeypatch):
    """Extra settings layered over the suite's own (watermark, the strip switch)."""
    real = config.get_settings
    extra: dict = {}
    monkeypatch.setattr(config, "get_settings", lambda: {**real(), **extra})
    return extra


@pytest.fixture(autouse=True)
def archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    init_db()
    conn = get_connection()
    for t in ("posting_log", "posting_queue", "publications"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    activity._reset_for_tests()
    return arch


class _Capture:
    """A stand-in poster that keeps the bytes it was handed, at validate time and at post time."""
    requires_mode = "any"
    supports_edit = True

    def __init__(self, code, result=None, raises=None):
        self.platform_id = code
        self.platform_name = code
        self._result = result or OK
        self._raises = raises
        self.sent: list[dict] = []
        self.validated: list[dict] = []

    @staticmethod
    def _grab(package):
        f, t = package.file_path, package.thumbnail_path
        return {"path": f, "thumb_path": t,
                "file": Path(f).read_bytes() if f else None,
                "thumb": Path(t).read_bytes() if t else None}

    def validate(self, package):
        self.validated.append(self._grab(package))
        return []

    async def post(self, package):
        self.sent.append(self._grab(package))
        if self._raises:
            raise self._raises
        return PostResult(**self._result)

    async def edit(self, ext_id, package):
        self.sent.append(self._grab(package))
        return PostResult(**self._result)

    async def _rate_limit(self):
        pass


def _stubs(monkeypatch, codes, **kw):
    stubs = {c: _Capture(c, **kw) for c in codes}
    monkeypatch.setattr(manager, "_get_poster", lambda p, account_id=None: stubs[p])
    return stubs


def _piece(image=None, name="pic.jpg", thumb=True):
    kw = {}
    if thumb:
        kw = {"thumbnail_filename": "thumb.png", "thumbnail_bytes": fx.png_with_text()}
    return artwork_reader.create_artwork(
        title="Sample Piece", image_filename=name, image_bytes=fx.gps_jpeg() if image is None else image,
        rating="general", tags={"default": ["a", "b", "c", "d", "e"]}, **kw)


def _folder_fingerprint(name):
    art = artwork_reader.load_artwork(name)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(art.path).iterdir() if p.is_file()}


def _leftovers(root):
    return [p.name for p in Path(root).iterdir() if p.name.startswith(("pp-clean-", "wm_"))]


def _log(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT platform, status, error_message FROM posting_log WHERE story_name = ? "
                            "ORDER BY log_id", (name,)).fetchall()
    finally:
        conn.close()


def _pubs(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT platform, format_file, file_hash FROM publications WHERE story_name = ?",
                            (name,)).fetchall()
    finally:
        conn.close()


# ── T018 artwork wiring ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_artwork_post_sends_no_details_render_and_thumbnail(monkeypatch, temp_root, settings):
    name = _piece()
    stubs = _stubs(monkeypatch, ["ib", "ws", "sf"])
    calls = []
    real = ms.CleanRun._clean
    monkeypatch.setattr(ms.CleanRun, "_clean", lambda self, p: calls.append(p) or real(self, p))
    res = await manager.post_artwork(name, ["ib", "ws", "sf"])
    assert all(r["success"] for r in res)
    sent = [s for st in stubs.values() for s in st.sent]
    assert len(sent) == 3
    for s in sent:
        assert fx.independent_kinds(s["file"]) == set()
        assert fx.independent_kinds(s["thumb"]) == set()
        assert Path(s["path"]).name == "pic.jpg" and Path(s["thumb_path"]).name == "thumb.png"
    assert len({s["path"] for s in sent}) == 1                              # one copy reused by every site
    assert len(calls) == 2                                                  # render + thumbnail, cleaned once
    assert _leftovers(temp_root) == []


@pytest.mark.asyncio
async def test_size_check_sees_the_cleaned_file(monkeypatch, temp_root, settings):
    name = _piece()
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    v = stubs["ib"].validated[0]
    assert "pp-clean-" in v["path"] and fx.independent_kinds(v["file"]) == set()


@pytest.mark.asyncio
async def test_a_fresh_run_cleans_again_from_the_library(monkeypatch, temp_root, settings):
    name = _piece()
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    await manager.post_artwork(name, ["ib"])
    a, b = stubs["ib"].sent
    assert a["path"] != b["path"] and a["file"] == b["file"]
    assert fx.independent_kinds(b["file"]) == set()


# ── T019 watermark ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watermark_lands_on_the_cleaned_copy(monkeypatch, temp_root, settings):
    settings.update(artwork_watermark_enabled=True, artwork_watermark_text="@SecondFur")
    name = _piece()
    stubs = _stubs(monkeypatch, ["ib"])
    seen = []
    from posting import watermark
    real = watermark.apply
    monkeypatch.setattr(watermark, "apply", lambda src, s=None: seen.append(src) or real(src, s))
    await manager.post_artwork(name, ["ib"])
    assert "pp-clean-" in seen[0]                                           # stamped onto the cleaned copy
    s = stubs["ib"].sent[0]
    assert Path(s["path"]).name.startswith("wm_")
    assert fx.independent_kinds(s["file"]) == set()
    assert _leftovers(temp_root) == []


@pytest.mark.parametrize("ext,make", [(".jpg", fx.plain_jpeg), (".png", lambda: fx.png_with_text())])
def test_watermark_keeps_the_icc_profile(tmp_path, temp_root, ext, make):
    from posting import watermark
    src = tmp_path / ("art" + ext)
    src.write_bytes(make())
    out, tmp = watermark.apply(str(src), {"artwork_watermark_enabled": True, "artwork_watermark_text": "@x"})
    assert tmp and out == tmp
    assert fx.pillow_icc(Path(out).read_bytes()) == fx.FAKE_ICC


def test_watermark_log_has_no_full_path(tmp_path, temp_root, caplog):
    from posting import watermark
    bad = tmp_path / "deep" / "broken.png"
    bad.parent.mkdir()
    bad.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    caplog.set_level(logging.INFO)
    out, tmp = watermark.apply(str(bad), {"artwork_watermark_enabled": True, "artwork_watermark_text": "@x"})
    assert out == str(bad) and tmp is None
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "broken.png" in text and str(bad.parent) not in text


# ── T020 the library and the records ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_library_fingerprint_unchanged_after_post(monkeypatch, temp_root, settings):
    settings.update(artwork_watermark_enabled=True, artwork_watermark_text="@SecondFur")
    name = _piece()
    before = _folder_fingerprint(name)
    _stubs(monkeypatch, ["ib", "ws"])
    await manager.post_artwork(name, ["ib", "ws"])
    assert _folder_fingerprint(name) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("wm", [False, True])
async def test_publication_row_keeps_the_library_path(monkeypatch, temp_root, settings, wm):
    from posting.sync import hash_file
    if wm:
        settings.update(artwork_watermark_enabled=True, artwork_watermark_text="@SecondFur")
    name = _piece()
    _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    lib = artwork_reader.load_artwork(name).image_path
    rows = _pubs(name)
    assert [r["format_file"] for r in rows] == [lib]
    assert rows[0]["file_hash"] == hash_file(lib)
    assert "pp-clean-" not in rows[0]["format_file"] and "wm_" not in rows[0]["format_file"]


# ── T021 nothing left behind ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError("the site blew up"), asyncio.CancelledError()],
                         ids=["error", "cancel"])
async def test_each_path_leaves_nothing_when_the_poster_raises_artwork(monkeypatch, temp_root, settings, exc):
    settings.update(artwork_watermark_enabled=True, artwork_watermark_text="@SecondFur")
    name = _piece()
    _stubs(monkeypatch, ["ib"], raises=exc)
    with pytest.raises(type(exc)):
        await manager.post_artwork(name, ["ib"])
    assert _leftovers(temp_root) == []


@pytest.mark.asyncio
async def test_watermark_temps_removed_when_posting_raises(monkeypatch, temp_root, settings):
    settings.update(artwork_watermark_enabled=True, artwork_watermark_text="@SecondFur",
                    posting_strip_metadata=False)
    name = _piece()
    _stubs(monkeypatch, ["ib"], raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await manager.post_artwork(name, ["ib"])
    assert _leftovers(temp_root) == []


# ── T022 honest results ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kept_picture_says_so_on_the_result(monkeypatch, temp_root, settings):
    data = fx.png_unknown_critical()
    name = _piece(data, name="pic.png", thumb=False)
    stubs = _stubs(monkeypatch, ["ib"])
    res = await manager.post_artwork(name, ["ib"])
    assert res[0]["success"] is True
    assert res[0]["note"] == ms.NOTE_KEPT and res[0]["metadata_kept"] is True
    assert stubs["ib"].sent[0]["file"] == data                              # sent as stored (FR-008)


@pytest.mark.asyncio
async def test_clean_result_has_no_note(monkeypatch, temp_root, settings):
    name = _piece()
    _stubs(monkeypatch, ["ib"])
    res = await manager.post_artwork(name, ["ib"])
    assert not res[0].get("note") and not res[0].get("metadata_kept")


def test_precheck_warns_without_writing_files(monkeypatch, temp_root, settings):
    name = _piece(fx.png_unknown_critical(), name="pic.png", thumb=False)
    _stubs(monkeypatch, ["ib", "ws", "sf"])
    reads = []
    real = ms.precheck_note
    monkeypatch.setattr(ms, "precheck_note", lambda p, s=None: reads.append(p) or real(p, s))
    before = sorted(p.name for p in temp_root.iterdir())
    r = manager.publish_check(name, ["ib", "ws", "sf"])
    assert r["kept_details"] == {"ib": ms.NOTE_KEPT, "ws": ms.NOTE_KEPT, "sf": ms.NOTE_KEPT}
    assert sorted(p.name for p in temp_root.iterdir()) == before
    assert len(reads) == 1                                                  # one read for three sites


def test_precheck_says_may_show_sideways(monkeypatch, temp_root, settings):
    monkeypatch.setattr(ms, "REDRAW_MAX_PIXELS", 100)
    name = _piece(fx.orientation_jpeg(6), thumb=False)
    _stubs(monkeypatch, ["ib"])
    assert manager.publish_check(name, ["ib"])["kept_details"] == {"ib": ms.NOTE_SIDEWAYS}


def test_precheck_quiet_for_a_cleanable_picture(monkeypatch, temp_root, settings):
    name = _piece()
    _stubs(monkeypatch, ["ib"])
    assert manager.publish_check(name, ["ib"])["kept_details"] == {}


# ── T022a the note outlives the screen ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kept_note_reaches_the_bell_line(monkeypatch, temp_root, settings):
    name = _piece(fx.png_unknown_critical(), name="pic.png", thumb=False)
    _stubs(monkeypatch, ["ib", "ws"])
    jid = activity.start("artwork", "Sample Piece", ["ib", "ws"])
    with activity.bound(jid):
        await manager.post_artwork(name, ["ib", "ws"])
    job = next(j for j in activity.snapshot() if j["id"] == jid)
    steps = {ln["key"]: (ln["state"], ln["step"]) for ln in job["lines"]}
    assert steps["ib"] == ("done", manager.KEPT_STEP) and steps["ws"] == ("done", manager.KEPT_STEP)


@pytest.mark.asyncio
async def test_kept_note_reaches_the_posting_log(monkeypatch, temp_root, settings):
    name = _piece(fx.png_unknown_critical(), name="pic.png", thumb=False)
    _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    row = _log(name)[0]
    assert row["status"] == "success" and ms.NOTE_KEPT in (row["error_message"] or "")


def _js(name):
    return (ROOT / "frontend" / "js" / name).read_text(encoding="utf-8")


def test_kept_note_shows_beside_attention_and_on_unconfirmed_rows():
    src = _js("components.js")
    i = src.index("publishResults(")
    body = src[i:i + 6000]
    assert "r.attention || r.note" not in body                             # the note no longer loses to attention
    assert "metadata_kept" in body
    assert re.search(r"unconfirmed", body)


def test_story_result_box_shows_the_note():
    src = _js("publish_check.js")
    i = src.index("publish-action-result-row")
    assert "r.note" in src[i:i + 2500]


def test_confirm_dialog_shows_kept_details():
    src = _js("components.js")
    i = src.index("async withPublishCheck(")
    assert "kept_details" in src[i:i + 1200]
    j = src.index("confirmPublish(o)")
    assert "keptDetails" in src[j:j + 3000]


# ── T023 the other ways out ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ig_host_reads_the_cleaned_bytes(monkeypatch, temp_root, settings):
    """The real Instagram poster, stopped at the hosting step: the file it hands the host ladder is the copy."""
    from posting import ig_host
    from posting.platforms.instagram import InstagramPoster
    name = _piece(thumb=False)
    poster = InstagramPoster()
    monkeypatch.setattr(poster, "_resolve_creds", lambda *a, **k: {"ig_access_token": "t", "ig_user_id": "1"})
    monkeypatch.setattr(poster, "validate", lambda package: [])          # no public host in a test
    monkeypatch.setattr(manager, "_get_poster", lambda p, account_id=None: poster)
    got = []

    async def fake_host(paths, s=None):
        got.extend(Path(p).read_bytes() for p in paths)  # noqa: ASYNC240
        raise ig_host.NoPublicHost("stopped by the test")

    monkeypatch.setattr(ig_host, "host_images", fake_host)
    await manager.post_artwork(name, ["ig"])
    assert got and all(fx.independent_kinds(b) == set() for b in got)


def _discord_preview(path):
    from posting import discord
    return discord.preview_bytes(str(path))


def test_discord_preview_carries_no_details(tmp_path, settings):
    for name, data in (("g.jpg", fx.gps_jpeg(orientation=6)), ("w.webp", fx.webp_with_exif_xmp(orientation=6))):
        p = tmp_path / name
        p.write_bytes(data)
        out, ext = _discord_preview(p)
        assert ext == "webp"
        names = [c for c, _ in fx.webp_chunks(out)]
        assert b"EXIF" not in names and b"XMP " not in names
        assert fx.independent_kinds(out) == set()


def test_discord_raw_fallback_never_sends_the_original(tmp_path, monkeypatch, settings):
    from PIL import ImageOps
    monkeypatch.setattr(ImageOps, "exif_transpose", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    p = tmp_path / "g.jpg"
    p.write_bytes(fx.gps_jpeg())
    out = _discord_preview(p)
    assert out is not None and out[1] == "jpg" and out[0] != p.read_bytes()
    assert fx.independent_kinds(out[0]) == set()
    bad = tmp_path / "z.png"
    bad.write_bytes(fx.png_unknown_critical())
    assert _discord_preview(bad) is None                                    # can't clean → no picture, not the original
    settings["posting_strip_metadata"] = False
    assert _discord_preview(p) == (p.read_bytes(), "jpg")                   # off = as today (FR-012)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_podcast_art_carries_no_details(tmp_path, temp_root, fmt):
    from posting import poster_fit
    p = tmp_path / "cover.jpg"
    p.write_bytes(fx.gps_jpeg())
    out = poster_fit.fit_poster(str(p), size=(64, 64), fmt=fmt)
    assert out and fx.independent_kinds(Path(out).read_bytes()) == set()


def test_ig_stash_carries_no_details(tmp_path, monkeypatch):
    from posting import ig_media
    monkeypatch.setattr(ig_media, "_dir", lambda: tmp_path)
    p = tmp_path / "g.png"
    p.write_bytes(fx.png_with_text())
    token = ig_media.stash_image(str(p))
    assert fx.independent_kinds((tmp_path / f"{token}.jpg").read_bytes()) == set()
    token2 = ig_media.stash_bytes(fx.gps_jpeg())
    assert fx.independent_kinds((tmp_path / f"{token2}.jpg").read_bytes()) == set()


# ── T017a start-up ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entry", ["server.py", "main.py"])
def test_startup_calls_the_sweep_inside_a_try(entry):
    src = (ROOT / entry).read_text(encoding="utf-8")
    m = re.search(r"try:\s*\n\s*from posting import metadata_strip\s*\n\s*metadata_strip\.sweep_stale\(\)\s*\n"
                  r"\s*except Exception", src)
    assert m, f"{entry} must call sweep_stale() inside a try"


# ── T032 stories ─────────────────────────────────────────────────────────────────────────────────────────────────────

STORY = "Test_Story"


@pytest.fixture
def story(story_archive, settings):
    settings["posting_story_archive_path"] = str(story_archive)
    return story_archive / STORY


def _cover(story_dir, data, name="test_story_thumbnail_full_series.jpg"):
    p = story_dir / name
    p.write_bytes(data)
    return p


@pytest.mark.asyncio
async def test_story_cover_goes_out_clean_on_post_and_update(monkeypatch, temp_root, story):
    lib = _cover(story, fx.gps_jpeg())
    stubs = _stubs(monkeypatch, ["ib"])
    res = await manager.post_story(STORY, ["ib"], chapters=[1])
    assert res[0]["success"] is True and not res[0].get("metadata_kept")
    res = await manager.update_story(STORY, platforms=["ib"], chapters=[1])
    assert res[0]["success"] is True
    assert len(stubs["ib"].sent) == 2
    for s in stubs["ib"].sent:
        assert fx.independent_kinds(s["thumb"]) == set()
        assert Path(s["thumb_path"]).name == lib.name and s["thumb_path"] != str(lib)
    assert _leftovers(temp_root) == []
    assert lib.read_bytes() == fx.gps_jpeg()                                # the library file is never written


@pytest.mark.asyncio
async def test_story_promo_card_goes_out_clean(monkeypatch, temp_root, story):
    from posting import story_reader
    card = _cover(story, fx.gps_jpeg(), name="card.jpg")
    real = story_reader.load_story

    def _with_card(*a, **k):
        st = real(*a, **k)
        st.announcement_image = str(card)
        return st
    monkeypatch.setattr(story_reader, "load_story", _with_card)
    stubs = _stubs(monkeypatch, ["bsky"])
    await manager.post_story(STORY, ["bsky"], chapters=[0])
    assert stubs["bsky"].sent and fx.independent_kinds(stubs["bsky"].sent[0]["thumb"]) == set()


@pytest.mark.asyncio
async def test_story_record_lines_hold_the_library_path(monkeypatch, temp_root, story):
    from posting.sync import hash_file
    _cover(story, fx.gps_jpeg())
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_story(STORY, ["ib"], chapters=[1])
    await manager.update_story(STORY, platforms=["ib"], chapters=[1])
    sent_file = stubs["ib"].sent[0]["path"]
    rows = _pubs(STORY)
    assert rows and all(r["format_file"] == sent_file for r in rows)        # the story text is never a copy
    assert all("pp-clean-" not in r["format_file"] for r in rows)
    assert rows[0]["file_hash"] == hash_file(sent_file)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError("the site blew up"), asyncio.CancelledError()],
                         ids=["error", "cancel"])
async def test_each_path_leaves_nothing_when_the_poster_raises_story(monkeypatch, temp_root, story, exc):
    _cover(story, fx.gps_jpeg())
    _stubs(monkeypatch, ["ib"], raises=exc)
    try:
        await manager.post_story(STORY, ["ib"], chapters=[1])
    except (RuntimeError, asyncio.CancelledError):
        pass                                                                 # post_story may record, not raise
    assert _leftovers(temp_root) == []


@pytest.mark.asyncio
async def test_kept_story_cover_says_so_on_post_and_update(monkeypatch, temp_root, story):
    _cover(story, fx.png_unknown_critical(), name="test_story_thumbnail_full_series.png")
    _stubs(monkeypatch, ["ib"])
    for res in (await manager.post_story(STORY, ["ib"], chapters=[1]),
                await manager.update_story(STORY, platforms=["ib"], chapters=[1])):
        assert res[0]["success"] is True
        assert res[0]["metadata_kept"] is True and ms.NOTE_KEPT in res[0]["note"]
    assert all(ms.NOTE_KEPT in (r["error_message"] or "") for r in _log(STORY))


@pytest.mark.asyncio
async def test_story_incoming_extras_never_reach_the_record(monkeypatch, temp_root, story):
    _cover(story, fx.gps_jpeg())
    _stubs(monkeypatch, ["ib"])
    bad = {"library_path": "/somewhere/else.txt", "metadata_note": "made up"}
    res = await manager.post_story(STORY, ["ib"], chapters=[1], extras=dict(bad))
    assert not res[0].get("note") and not res[0].get("metadata_kept")
    assert all(r["format_file"] != bad["library_path"] for r in _pubs(STORY))


# ── T033 posts ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _post_with_media(tmp_path, pictures):
    from database import posts_queries
    conn = get_connection()
    try:
        pid = posts_queries.create_post(conn, body="A sample post", now="2026-11-04T00:00:00")
        for i, (name, data, alt) in enumerate(pictures):
            p = tmp_path / name
            p.write_bytes(data)
            posts_queries.add_post_media(conn, post_id=pid, ordinal=i, path=str(p), alt=alt)
    finally:
        conn.close()
    return pid


@pytest.fixture
def post_spy(monkeypatch):
    """Stand in for the per-site send, the paired-comment pass and the Discord line."""
    from posting import discord, paired_comment, post_publisher
    spy = {"sent": [], "comment_post": None, "announce": None}

    async def _one(post, platform, account_id, settings):
        spy["sent"].append([{"path": m["path"], "alt": m.get("alt", ""), "bytes": Path(m["path"]).read_bytes()}  # noqa: ASYNC240
                            for m in post.get("media") or []])
        return {"platform": platform, "account_id": 0, "success": True, "external_id": "1",
                "external_url": "https://example.com/1", "error": ""}

    async def _comments(post, results, settings):
        spy["comment_post"] = post

    async def _announce(**kw):
        p = kw.get("image_path")
        spy["announce"] = Path(p).read_bytes() if p else None  # noqa: ASYNC240

    monkeypatch.setattr(post_publisher, "_publish_one", _one)
    monkeypatch.setattr(paired_comment, "comment_pass_posts", _comments)
    monkeypatch.setattr(discord, "announce_publish", _announce)
    return spy


@pytest.mark.asyncio
async def test_post_pictures_go_out_clean_in_order_with_alt(tmp_path, temp_root, settings, post_spy):
    from database import posts_queries
    from posting import post_publisher
    pid = _post_with_media(tmp_path, [("one.jpg", fx.gps_jpeg(), "first alt"), ("two.png", fx.png_with_text(), "second")])
    conn = get_connection()
    try:
        before = posts_queries.get_post(conn, pid)
    finally:
        conn.close()
    res = await post_publisher.publish_post(pid, ["bsky", "mast"])
    assert all(r["success"] for r in res) and not any(r.get("metadata_kept") for r in res)
    for sent in post_spy["sent"]:
        assert [Path(m["path"]).name for m in sent] == ["one.jpg", "two.png"]
        assert [m["alt"] for m in sent] == ["first alt", "second"]
        assert all(fx.independent_kinds(m["bytes"]) == set() for m in sent)
        assert all("pp-clean-" in m["path"] for m in sent)
    assert fx.independent_kinds(post_spy["announce"]) == set()              # Discord reads the cleaned first picture
    # The stored rows and the dict handed to the paired comments keep library paths (FR-SEC-008).
    conn = get_connection()
    try:
        after = posts_queries.get_post(conn, pid)
    finally:
        conn.close()
    assert [m["path"] for m in after["media"]] == [m["path"] for m in before["media"]]
    assert [m["path"] for m in post_spy["comment_post"]["media"]] == [m["path"] for m in before["media"]]
    assert _leftovers(temp_root) == []


def test_legacy_image_path_post_is_cleaned(tmp_path, temp_root):
    from posting import post_publisher
    p = tmp_path / "legacy.jpg"
    p.write_bytes(fx.gps_jpeg())
    post = {"body": "x", "image_path": str(p)}
    with ms.CleanRun() as run:
        send, note = post_publisher._cleaned_post(post, run)
        assert note is None and send["image_path"] != str(p)
        assert fx.independent_kinds(Path(send["image_path"]).read_bytes()) == set()
    assert post == {"body": "x", "image_path": str(p)}                      # the original is untouched


@pytest.mark.asyncio
async def test_kept_post_picture_sets_note_never_attention(tmp_path, temp_root, settings, post_spy):
    from database import posts_queries
    from posting import post_publisher
    pid = _post_with_media(tmp_path, [("odd.png", fx.png_unknown_critical(), "")])
    res = await post_publisher.publish_post(pid, ["bsky"])
    assert res[0]["metadata_kept"] is True and ms.NOTE_KEPT in res[0]["note"]
    assert not res[0].get("attention")
    conn = get_connection()
    try:
        pub = [p for p in posts_queries.get_post_publications(conn, pid) if p["platform"] == "bsky"][0]
    finally:
        conn.close()
    assert pub["status"] == "posted"                                         # not a draft


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError("the site blew up"), asyncio.CancelledError()],
                         ids=["error", "cancel"])
async def test_each_path_leaves_nothing_when_the_poster_raises_post(tmp_path, monkeypatch, temp_root, settings,
                                                                   post_spy, exc):
    from posting import post_publisher
    pid = _post_with_media(tmp_path, [("one.jpg", fx.gps_jpeg(), "")])

    async def _boom(*a, **k):
        raise exc
    monkeypatch.setattr(post_publisher, "_publish_one", _boom)
    with pytest.raises(type(exc)):
        await post_publisher.publish_post(pid, ["bsky"])
    assert _leftovers(temp_root) == []

# ── T038 the setting ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,saved", [(True, True), (False, False), (0, None), ("", None), ("false", None),
                                         (None, None), (1, None)])
def test_strip_setting_accepts_only_booleans(monkeypatch, value, saved):
    from routes import posting_api
    got = {}
    monkeypatch.setattr(config, "save_settings", lambda d: got.update(d))
    posting_api.save_posting_settings({"posting_strip_metadata": value})
    assert got.get("posting_strip_metadata") is saved                      # `is`: 1 and "false" never pass as bools
    assert ("posting_strip_metadata" in got) == (saved is not None)


def test_strip_setting_defaults_on(monkeypatch):
    from routes import posting_api
    monkeypatch.setattr(config, "get_settings", lambda: {})
    assert posting_api.get_posting_settings()["posting_strip_metadata"] is True
    monkeypatch.setattr(config, "get_settings", lambda: {"posting_strip_metadata": False})
    assert posting_api.get_posting_settings()["posting_strip_metadata"] is False


@pytest.mark.asyncio
async def test_setting_off_sends_the_stored_bytes_artwork(monkeypatch, temp_root, settings):
    settings["posting_strip_metadata"] = False
    name = _piece(thumb=False)
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    lib = artwork_reader.load_artwork(name).image_path
    assert stubs["ib"].sent[0]["file"] == Path(lib).read_bytes() and stubs["ib"].sent[0]["path"] == lib  # noqa: ASYNC240
    assert _leftovers(temp_root) == []


@pytest.mark.asyncio
async def test_setting_off_sends_the_stored_bytes_story(monkeypatch, temp_root, story, settings):
    settings["posting_strip_metadata"] = False
    lib = _cover(story, fx.gps_jpeg())
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_story(STORY, ["ib"], chapters=[1])
    assert stubs["ib"].sent[0]["thumb_path"] == str(lib) and stubs["ib"].sent[0]["thumb"] == fx.gps_jpeg()


@pytest.mark.asyncio
async def test_setting_off_sends_the_stored_bytes_post(tmp_path, temp_root, settings, post_spy):
    from posting import post_publisher
    settings["posting_strip_metadata"] = False
    pid = _post_with_media(tmp_path, [("one.jpg", fx.gps_jpeg(), "")])
    await post_publisher.publish_post(pid, ["bsky"])
    assert post_spy["sent"][0][0]["path"] == str(tmp_path / "one.jpg")
    assert post_spy["sent"][0][0]["bytes"] == fx.gps_jpeg()


@pytest.mark.asyncio
async def test_scheduled_post_reads_the_setting_when_it_fires(tmp_path, temp_root, settings, post_spy):
    """The switch is read when the row fires, not when it was scheduled."""
    from database import posting_queries
    from posting import scheduler
    pid = _post_with_media(tmp_path, [("one.jpg", fx.gps_jpeg(), "")])
    for on in (True, False):
        settings.pop("posting_strip_metadata", None)                         # on when scheduled
        conn = get_connection()
        try:
            posting_queries.add_to_queue(conn, str(pid), 0, "bsky", action="post", content_type="post",
                                         title_override="A sample post")
            item = [q for q in posting_queries.get_queue(conn, content_type="post") if q["status"] == "pending"][0]
        finally:
            conn.close()
        settings["posting_strip_metadata"] = on                              # flipped before it fires
        await scheduler._process_queue_item(item)
        sent = post_spy["sent"][-1][0]["bytes"]
        assert (sent == fx.gps_jpeg()) is (not on)
        assert fx.independent_kinds(sent) == (set() if on else fx.independent_kinds(fx.gps_jpeg()))

# ── After_implement review (2026-11-04) ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metadata_only_artwork_sync_still_sends_a_clean_cover(monkeypatch, temp_root, settings):
    """Weasyl re-uploads a cover on a metadata-only edit (impl review High): whatever goes must be the clean copy."""
    name = _piece()
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_artwork(name, ["ib"])
    res = await manager.update_artwork(name, platforms=["ib"])
    edit = stubs["ib"].sent[-1]
    assert edit["thumb"] is not None and fx.independent_kinds(edit["thumb"]) == set()
    assert not res[0].get("metadata_kept")                                  # nothing claimed for a metadata push
    assert _leftovers(temp_root) == []


@pytest.mark.asyncio
async def test_metadata_only_story_edit_still_sends_a_clean_cover(monkeypatch, temp_root, story):
    _cover(story, fx.gps_jpeg())
    stubs = _stubs(monkeypatch, ["ib"])
    await manager.post_story(STORY, ["ib"], chapters=[1])
    await manager.update_story(STORY, platforms=["ib"], chapters=[1], extras={"skip_content_refresh": True})
    assert fx.independent_kinds(stubs["ib"].sent[-1]["thumb"]) == set()


def test_a_run_of_fill_bytes_is_skipped_fast():
    import time
    t = time.monotonic()
    assert ms.clean_bytes(b"\xff\xd8" + b"\xff" * 20_000_000) is None       # truncated → kept, quickly
    assert time.monotonic() - t < 2


def test_too_many_scans_keeps_the_rotation_note(tmp_path, temp_root, monkeypatch):
    monkeypatch.setattr(ms, "REDRAW_MAX_SCANS", 0)
    p = tmp_path / "r.jpg"
    p.write_bytes(fx.orientation_jpeg(6))
    with ms.CleanRun() as run:
        o = run.clean(p)
    assert o.state == "cleaned" and o.note == ms.NOTE_SIDEWAYS
    assert ms.precheck_note(str(p), {}) == ms.NOTE_SIDEWAYS