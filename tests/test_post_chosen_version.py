"""Post the version you chose (spec 063, POSTVERSION).

Viewing an alternate version on a piece's page and pressing Post sent the original: the page never recorded the
chosen version, Post sent none, and the confirm thumbnail showed the alternate. Post now asks "Which versions to
post" with the viewed one ticked; Schedule offers one version and stores it on the queue row.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from posting import artwork_reader, manager

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "frontend" / "js"
NODE = shutil.which("node")


def _read(name):
    return (JS / name).read_text(encoding="utf-8")


def _fn(src: str, head: str, size: int = 4000) -> str:
    """The text of one method, from its head to the next method at the same indent (or `size` chars)."""
    i = src.index(head)
    nxt = re.search(r"\n    (?:async )?[_a-zA-Z]\w*\([^)]*\) \{", src[i + len(head):])
    return src[i: i + len(head) + (nxt.start() if nxt else size)]


# ── T001: a piece with a declared version ─────────────────────────────────────────────────────────

VARIANTS = [{"key": "", "label": "Primary", "image": "a.png", "rating": ""},
            {"key": "clean", "label": "Clean <b>", "image": "b.png", "rating": ""}]


@pytest.fixture
def piece(tmp_path, monkeypatch):
    for f in ("a.png", "b.png"):
        (tmp_path / f).write_bytes(b"\x89PNG\r\n\x1a\n")
    art = artwork_reader.ArtworkInfo(name="Sample_Piece", path=tmp_path, title="Sample Piece", description="",
                                     author="", rating="general", image="a.png", variants=list(VARIANTS))
    from routes import artwork_api
    monkeypatch.setattr(artwork_api.artwork_reader, "load_artwork", lambda name: art)
    monkeypatch.setattr(artwork_api, "_store_comments", lambda *a, **k: None)
    return art


def _node(js: str):
    """Evaluate artwork.js in node and run `js` against `window.Artwork`; returns its JSON output."""
    script = ("global.window={sortByPlatformName:a=>a};global.document={addEventListener(){}};"
              "eval(require('fs').readFileSync(" + json.dumps(str(JS / "artwork.js")) + ",'utf8'));"
              "const A=window.Artwork;console.log(JSON.stringify(" + js + "));")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


META = {"variants": VARIANTS}


def _ticked(rows):
    return [r["key"] for r in rows if r.get("checked")]


# ── T002: the picker list ─────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_viewing_a_version_ticks_it_not_automatic():
    rows = _node(f"A._pubRenders({json.dumps(META)}, 'clean')")
    assert _ticked(rows) == ["clean"]
    assert [r["key"] for r in rows] == ["__auto__", "__primary__", "clean"]


@pytest.mark.skipif(NODE is None, reason="node is needed")
@pytest.mark.parametrize("view", ["''", "null", "undefined"])
def test_viewing_the_original_keeps_automatic(view):
    assert _ticked(_node(f"A._pubRenders({json.dumps(META)}, {view})")) == ["__auto__"]


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_an_unknown_view_key_keeps_automatic():
    assert _ticked(_node(f"A._pubRenders({json.dumps(META)}, 'nope')")) == ["__auto__"]


# ── T004: the piece page and the dialog ───────────────────────────────────────────────────────────

def test_piece_page_remembers_the_viewed_version():
    src = _read("masterpieces.js")
    view = _fn(src, "_detailView(name, m) {")
    assert "key: v.key || ''" in view and "key: i === 0 ? '' : null" in view   # declared vs folder pictures
    assert 'data-mp-vkey="' in src
    paint = _fn(src, "_paintDetail(name, m) {", 2500)
    assert "this._viewKey = " in paint
    click = src[src.index("const alt = e.target.closest('[data-mp-img]');"):][:1500]
    assert "this._viewKey = " in click and "mpVkey" in click


def test_piece_page_post_sends_the_ticked_versions():
    src = _read("masterpieces.js")
    choice = _fn(src, "_versionChoice(name) {")
    assert "_pubRenders(m, view)" in choice and "this._viewKey" in choice
    assert "isn't a version of the piece yet" in choice                  # FR-009
    pub = _fn(src, "async _publishNow(name) {", 6000)
    assert "this._versionChoice(name)" in pub
    assert "renders: choice.renders" in pub and "renderThumbs: choice.renderThumbs" in pub
    assert "thumb: choice.thumb" in pub and "renders: conf.renders" in pub
    assert "mp-hero-img" not in pub                                     # the old thumbnail source


def test_confirm_thumbnail_follows_the_ticks():
    src = _read("components.js")
    assert "o.renderThumbs" in src
    i = src.index("if (thumbEl && o.renderThumbs)")
    block = src[i - 300: i + 600]
    assert ".pub-confirm-thumb" in block and ".src = " in block           # a property, never markup
    assert "innerHTML" not in block


def test_new_upload_dialog_is_unchanged():
    assert "renders: this._pubRenders(meta)," in _read("artwork.js")


# ── T009: the schedule route ──────────────────────────────────────────────────────────────────────

def _schedule(**extra):
    from routes import artwork_api
    body = {"artwork_name": "Sample_Piece", "platform": "ib", "scheduled_at": "2031-01-06T09:00:00Z", **extra}
    return asyncio.run(artwork_api.schedule_artwork(body))


def _rows():
    from database.db import get_connection
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT variant_key FROM posting_queue WHERE story_name = 'Sample_Piece'").fetchall()]
    finally:
        conn.close()


def test_schedule_stores_a_declared_version(piece):
    _schedule(variant_key="clean")
    assert _rows() == [{"variant_key": "clean"}]


@pytest.mark.parametrize("extra", [{}, {"variant_key": None}, {"variant_key": ""}])
def test_schedule_without_a_version_is_automatic(piece, extra):
    _schedule(**extra)
    assert _rows() == [{"variant_key": ""}]


def test_schedule_the_original_stores_primary_not_blank(piece):
    """On a queue row "" means Automatic (the rating's pick), so "the original" must be stored as __primary__."""
    _schedule(variant_key="__primary__")
    assert _rows() == [{"variant_key": "__primary__"}]


def test_schedule_refuses_a_version_the_piece_does_not_have(piece):
    with pytest.raises(HTTPException) as e:
        _schedule(variant_key="nope")
    assert e.value.status_code == 400 and e.value.detail == "This piece has no version called 'nope'."
    assert _rows() == []


@pytest.mark.parametrize("bad", ["../b.png", "b.png"])
def test_schedule_version_is_never_a_path(piece, bad):
    with pytest.raises(HTTPException) as e:
        _schedule(variant_key=bad)
    assert e.value.status_code == 400 and _rows() == []


@pytest.mark.parametrize("bad", ["x" * 33, 5, ["clean"], {"k": 1}])
def test_schedule_refuses_a_long_or_odd_version_name(piece, bad):
    with pytest.raises(HTTPException) as e:
        _schedule(variant_key=bad)
    assert e.value.status_code == 400 and e.value.detail == "That isn't a version name."
    assert _rows() == []


# ── T011: what a scheduled version does when it fires ────────────────────────────────────────────

def test_scheduled_version_reaches_the_poster_as_its_override(piece, monkeypatch):
    from database import posting_queries
    from database.db import get_connection
    from posting import scheduler
    _schedule(variant_key="clean")
    seen = {}

    async def _post_artwork(name, platforms, **kw):
        seen.update(name=name, platforms=platforms, **kw)
        return [{"platform": "ib", "success": True, "external_id": "1", "external_url": "https://example.com/1"}]
    monkeypatch.setattr(manager, "post_artwork", _post_artwork)
    conn = get_connection()
    try:
        item = posting_queries.get_queue(conn, story_name="Sample_Piece", content_type="artwork")[0]
    finally:
        conn.close()
    asyncio.run(scheduler._process_queue_item(item))
    assert seen["variant_overrides"] == {"ib": "clean"}


def test_a_scheduled_version_renamed_away_is_refused_by_name(piece):
    results = []
    poster = type("P", (), {"max_rating": "adult"})()
    assert manager._site_renders(piece, "ib", poster, {"ib": "gone"}, None, results) == []
    assert len(results) == 1 and "gone" in str(results[0]) and not results[0].get("success")


# ── T015: labels are the owner's text ─────────────────────────────────────────────────────────────

def test_version_labels_are_escaped():
    src = _read("masterpieces.js")
    form = src[src.index('id="mp-schedule-version"') - 600:][:2000]
    assert "this.esc(" in form and "<option" in form
    listing = _fn(src, "async _loadScheduled(name) {")
    assert "variant_key" in listing and "this.esc(" in listing
    assert "esc(r.label)" in _read("components.js")                    # the picker (existing)


def test_scheduled_list_names_the_site():
    """The walk found "🕔 — Clean — 4:45 am": PLATFORMS entries carry `label`, and `plat.name` was undefined."""
    listing = _fn(_read("masterpieces.js"), "async _loadScheduled(name) {")
    assert "plat.label" in listing and "plat.name" not in listing
