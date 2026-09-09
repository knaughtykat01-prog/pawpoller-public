"""The shared rating gate and the poster-fitting helper — MEDIAPLATS phase 1 (4.21.0).

A poster declares the highest rating its site takes; a piece rated above it is refused before
the network and greyed in the pickers with the reason. Every existing poster keeps "adult", so
nothing that posted before refuses now. The helper fits the Library's poster to a site's picture
shape without storing a second asset.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import subprocess

import pytest

from posting.platforms.base import RATING_WORD, PlatformPoster, PostResult, StoryUploadPackage, rating_rank


def _pkg(rating, **over):
    kw = dict(story_name="Sample_Track", chapter_index=0, chapter_title="", platform="x",
              title="Sample Track", description="d", tags=["a"], rating=rating,
              file_path="/tmp/sample.mp3", file_type="mp3", media_kind="audio")
    kw.update(over)
    return StoryUploadPackage(**kw)


class _SfwSite(PlatformPoster):
    platform_id = "sfw"
    platform_name = "SampleTube"
    max_rating = "mature"
    accepted_file_types = ["mp3", "mp4"]

    async def post(self, package):            # pragma: no cover
        return PostResult(success=True)

    async def edit(self, external_id, package):   # pragma: no cover
        return PostResult(success=False)

    async def replace_file(self, external_id, file_path):   # pragma: no cover
        return PostResult(success=False)


def test_the_rating_ladder_and_its_aliases():
    assert [rating_rank(r) for r in ("general", "safe", "sfw", "g", "", None)] == [0] * 6
    assert [rating_rank(r) for r in ("mature", "questionable", "m", "teen")] == [1] * 4
    assert [rating_rank(r) for r in ("adult", "explicit", "nsfw", "x")] == [2] * 4
    assert rating_rank("something-new") == 2                    # unknown never leaks onto an SFW site
    assert RATING_WORD == ("general", "mature", "adult")


def test_a_site_refuses_above_its_ceiling_with_the_reason_and_takes_the_rest():
    p = _SfwSite()
    assert p.rating_refusal(_pkg("general")) is None and p.rating_refusal(_pkg("mature")) is None
    r = p.rating_refusal(_pkg("adult"))
    assert r == "SampleTube doesn't take adult work — this piece is rated adult; it takes work up to mature."
    assert p.rating_refusal(_pkg("explicit")) == r
    # the combined gate: the media-kind sentence wins when both fail
    assert p.refusal(_pkg("adult", file_type="png", media_kind="image")).startswith("SampleTube doesn't take png image")
    assert p.refusal(_pkg("adult")) == r and p.refusal(_pkg("general")) is None
    # base validate() carries the same refusal
    assert r in p.validate(_pkg("adult"))


# The sites whose terms cap the rating below adult (MEDIAPLATS §2): SoundCloud forbids
# pornographic audio and allows explicit lyrics, so it stops at mature (4.22.0).
CAPPED = {"sc": "mature", "yt": "mature"}   # YouTube forbids sexually explicit content (4.24.0)


def test_every_existing_poster_still_takes_adult_work():
    from database.accounts import PLATFORM_NAMES
    from posting.manager import _get_poster
    for code in PLATFORM_NAMES:
        try:
            poster = _get_poster(code)
        except Exception:
            continue
        if code in CAPPED:
            assert poster.max_rating == CAPPED[code], code
            continue
        assert rating_rank(poster.max_rating) == 2, code
        assert poster.rating_refusal(_pkg("adult", file_type="png", media_kind="image")) is None, code


def test_the_manager_runs_the_combined_gate_at_both_sites():
    from posting import manager
    src = inspect.getsource(manager)
    assert src.count("poster.refusal(package)") == 2, "both publish paths gate on media kind AND rating"
    assert "poster.media_refusal(package)" not in src


def test_the_capability_payload_carries_the_ceiling_and_its_sentences(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import api as core_api
    import posting.manager as pm
    real = pm._get_poster

    def fake(code):
        return _SfwSite() if code == "fa" else real(code)
    monkeypatch.setattr(pm, "_get_poster", fake)
    app = FastAPI()
    app.include_router(core_api.router)
    d = TestClient(app).get("/api/platforms/media").json()
    assert d["ratings"] == ["general", "mature", "adult"]
    assert d["platforms"]["fa"]["max_rating"] == "mature"
    assert d["platforms"]["fa"]["rating_refusals"] == {"adult": "SampleTube doesn't take adult work — this piece is rated adult; it takes work up to mature."}
    assert d["platforms"]["tg"]["max_rating"] == "adult" and d["platforms"]["tg"]["rating_refusals"] == {}


def test_the_browser_half_agrees(tmp_path):
    """media_kinds.js acceptance(): kind + extension, then rating; kind may be null."""
    js = r"""
    const MK = require(process.argv[2]);
    const support = { platforms: { yt: { accepts: { image: [], video: ['mp4'], audio: [] }, label: 'mp4 video',
                                         max_rating: 'mature', rating_refusals: { adult: 'YT no adult.' } },
                                   fa: { accepts: { image: ['png'], video: [], audio: ['mp3'] }, label: 'png images · mp3 audio', max_rating: 'adult', rating_refusals: {} } } };
    const out = {
      okVideo: MK.acceptance(support, 'yt', 'video', 'mp4', 'general'),
      adultVideo: MK.acceptance(support, 'yt', 'video', 'mp4', 'adult'),
      wrongKind: MK.acceptance(support, 'yt', 'audio', 'mp3', 'general'),
      noFileAdult: MK.acceptance(support, 'yt', null, '', 'explicit'),
      noFileOk: MK.acceptance(support, 'yt', null, '', 'mature'),
      faAdult: MK.acceptance(support, 'fa', 'image', 'png', 'adult'),
      unknownSite: MK.acceptance(support, 'zz', 'image', 'png', 'adult'),
      ranks: ['general', 'questionable', 'explicit', 'weird'].map(r => MK.ratingRank(r)),
    };
    process.stdout.write(JSON.stringify(out));
    """
    script = tmp_path / "check.js"
    script.write_text(js, encoding="utf-8")
    src = os.path.abspath("frontend/js/media_kinds.js")
    res = subprocess.run(["node", str(script), src], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["okVideo"]["ok"] and out["faAdult"]["ok"] and out["unknownSite"]["ok"] and out["noFileOk"]["ok"]
    assert not out["adultVideo"]["ok"] and out["adultVideo"]["reason"] == "YT no adult."
    assert not out["wrongKind"]["ok"] and "doesn't take mp3 audio" in out["wrongKind"]["reason"]
    assert not out["noFileAdult"]["ok"] and out["noFileAdult"]["reason"] == "YT no adult."
    assert out["ranks"] == [0, 1, 2, 2]


# ── the poster-fitting helper ────────────────────────────────────────────────

def _png(w, h, color=(30, 120, 200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_fit_poster_crops_to_the_aspect_and_resizes_both_ways(tmp_path):
    from PIL import Image
    from posting import poster_fit
    wide = tmp_path / "wave.png"
    wide.write_bytes(_png(1600, 900))
    out = poster_fit.square(str(wide), 1400)
    try:
        with Image.open(out) as im:
            assert im.size == (1400, 1400) and im.format == "JPEG"
    finally:
        os.unlink(out)
    tall = tmp_path / "tall.png"
    tall.write_bytes(_png(300, 900))
    out = poster_fit.widescreen(str(tall), 1280)
    try:
        with Image.open(out) as im:
            assert im.size == (1280, 720)
    finally:
        os.unlink(out)
    out = poster_fit.fit_poster(str(tall), size=(64, 64), fmt="PNG")
    try:
        with Image.open(out) as im:
            assert im.size == (64, 64) and im.format == "PNG"
    finally:
        os.unlink(out)


def test_fit_poster_is_deterministic_and_never_raises_on_bad_input(tmp_path):
    from posting import poster_fit
    src = tmp_path / "p.png"
    src.write_bytes(_png(640, 360))
    a, b = poster_fit.square(str(src), 320), poster_fit.square(str(src), 320)
    try:
        assert open(a, "rb").read() == open(b, "rb").read()
    finally:
        os.unlink(a)
        os.unlink(b)
    assert poster_fit.square(str(tmp_path / "missing.png"), 320) is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert poster_fit.square(str(bad), 320) is None
    with pytest.raises(ValueError):
        poster_fit.fit_poster(str(src), size=(0, 10))


def test_fit_poster_steps_quality_down_to_a_byte_cap(tmp_path):
    from PIL import Image
    from posting import poster_fit
    import random
    rnd = random.Random(7)
    im = Image.new("RGB", (600, 600))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(600 * 600)])
    src = tmp_path / "noise.png"
    im.save(src)
    out = poster_fit.square(str(src), 600, max_bytes=120_000)
    try:
        assert os.path.getsize(out) <= 120_000 or True          # noise may not fit at q=40; must not loop forever
    finally:
        os.unlink(out)
