"""Editing a DeviantArt ARTWORK must not go looking for literature (3.34.0).

`post()` has always branched image file types to the Sta.sh path. `edit()` did
not branch at all, so an artwork sync fell through to the literature path and
tried to read the image as UTF-8 text. Observed on prod 2026-09-02 during a
Sync-to-sites run:

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0

0xFF is a JPEG's SOI marker. This is the SoFurry 3.9.5 bug on a second platform,
with the same collateral: `update_artwork` recorded the failure against a live,
correctly-posted deviation and flipped its publication row to `failed`.

⚠ **3.34.0 also drew a wrong conclusion, corrected in 3.36.0.** It read our own
client's endpoint list, found only `deviation/literature/update/{id}`, and wrote
that up as a limit of DA's API. DA in fact exposes `POST /deviation/edit/{id}`
for any deviation type; the description (which that endpoint does not carry)
goes through the editor's own `_napi/shared_api/deviation/update`. So
`supports_artwork_edit` is True again and artwork edits do real work.

What survives from 3.34.0 is the part that was actually measured: `edit()` must
never open an artwork file as UTF-8, and a failure must not be recorded against
a live deviation. Those guards are kept below.
"""
from __future__ import annotations

import inspect

import pytest

from posting.platforms import deviantart as da_module
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from posting.platforms.deviantart import DeviantArtPoster


def _package(file_type: str, path: str = "/tmp/piece", **over) -> StoryUploadPackage:
    kw = dict(
        story_name="Some_Piece", chapter_index=0, chapter_title="",
        platform="da", title="A Title", description="desc",
        tags=["a", "b"], rating="mature",
        file_path=f"{path}.{file_type}", file_type=file_type,
    )
    kw.update(over)
    return StoryUploadPackage(**kw)


@pytest.fixture
def poster(monkeypatch):
    """A poster whose client would fail loudly if the literature path ran."""
    p = DeviantArtPoster()

    async def _ensure():
        raise AssertionError("artwork edit must not reach the literature client")

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


# ── the capability split ─────────────────────────────────────────────────────

def test_da_can_edit_both_literature_and_artwork():
    """3.34.0 asserted False here on a premise that turned out to be wrong."""
    assert DeviantArtPoster.supports_edit is True
    assert DeviantArtPoster.supports_artwork_edit is True, (
        "DA exposes POST /deviation/edit/{id} for image deviations too"
    )


def test_every_other_poster_defaults_to_editing_artwork():
    """The default must stay True or the split silently disables working syncs."""
    assert PlatformPoster.supports_artwork_edit is True

    from posting.platforms.e621 import E621Poster
    from posting.platforms.furaffinity import FurAffinityPoster
    from posting.platforms.inkbunny import InkbunnyPoster
    from posting.platforms.sofurry import SoFurryPoster
    from posting.platforms.weasyl import WeasylPoster

    for cls in (FurAffinityPoster, InkbunnyPoster, SoFurryPoster, E621Poster, WeasylPoster):
        assert cls.supports_artwork_edit is True, cls.__name__


# ── the crash itself ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "gif", "webp"])
async def test_editing_artwork_never_opens_the_file(ext, monkeypatch):
    """THE regression guard, and the one thing 3.34.0 measured correctly.

    The prod crash was `open(<jpeg>, 'r', encoding='utf-8')` inside `edit()`.
    The artwork path now does real work instead of refusing, so this asserts the
    same thing about the working path: any open() of the artwork file is the bug
    returning.
    """
    import builtins

    real_open = builtins.open

    def _guard(path, *a, **kw):
        if str(path).startswith("/tmp/piece"):
            raise AssertionError(f"edit() must not open the artwork file ({path})")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", _guard)

    class _Client:
        async def uuid_for(self, e):
            return e

        async def oauth_edit_deviation(self, dev_id, **kw):
            return {}

        async def napi_set_description(self, e, text, csrf_token=""):
            return {}

    p = DeviantArtPoster()

    async def _ensure():
        return _Client(), "tok"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_deviation_url", lambda *a, **kw: "https://da/x")

    result = await p.edit("1375013761", _package(ext))

    assert result.success is True
    assert result.external_id == "1375013761"


@pytest.mark.asyncio
async def test_a_real_jpeg_does_not_raise(poster, tmp_path):
    """End to end on actual JPEG bytes — 0xFF D8 is what crashed prod."""
    img = tmp_path / "piece.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")

    result = await poster.edit("1375013761", _package("jpg", path=str(tmp_path / "piece")))

    # Whatever happens, it must never be a UnicodeDecodeError from reading the
    # image as text — that was the prod crash.
    assert "codec" not in (result.error or "")


@pytest.mark.asyncio
async def test_replace_file_refuses_an_image_too(poster):
    """Same UTF-8 read, same trap, reachable independently of edit()."""
    result = await poster.replace_file("1375013761", "/tmp/piece.png")
    assert result.success is False
    assert "image" in (result.error or "").lower()


def test_the_guard_shares_post_s_image_test():
    """If post() and edit() disagree about what an image is, the bug returns."""
    src = inspect.getsource(da_module)
    assert src.count("_IMAGE_TYPES") >= 3, (
        "post(), edit() and replace_file() should all test the one constant"
    )
    assert da_module._IMAGE_TYPES == ("png", "jpg", "jpeg", "gif", "webp")


# ── literature still works, and now honours skip_content_refresh ─────────────

@pytest.mark.asyncio
async def test_a_metadata_only_story_edit_does_not_read_the_file(monkeypatch, tmp_path):
    """Sync-all sets skip_content_refresh on every member. FA/IB/SF honoured it;
    DA ignored it, which is how a package's file reached open() at all."""
    p = DeviantArtPoster()
    story = tmp_path / "s.txt"
    story.write_text("body text", encoding="utf-8")

    sent = {}

    class _Client:
        async def uuid_for(self, ext_id):
            return ext_id

        async def oauth_update_literature(self, dev_id, **kw):
            sent.update(kw)
            return {"url": "https://www.deviantart.com/x"}

    async def _ensure():
        return _Client(), "tok"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_deviation_url", lambda *a, **kw: "https://www.deviantart.com/x")

    pkg = _package("txt", path=str(tmp_path / "s"))
    pkg.file_path = str(story)
    pkg.extra["skip_content_refresh"] = True

    result = await p.edit("123", pkg)

    assert result.success is True
    assert sent.get("body") is None, "metadata-only edit must not send file content"


@pytest.mark.asyncio
async def test_a_full_story_edit_still_sends_the_body(monkeypatch, tmp_path):
    """The skip must not break the path it sits in front of."""
    p = DeviantArtPoster()
    story = tmp_path / "s.txt"
    story.write_text("body text", encoding="utf-8")

    sent = {}

    class _Client:
        async def uuid_for(self, ext_id):
            return ext_id

        async def oauth_update_literature(self, dev_id, **kw):
            sent.update(kw)
            return {"url": "https://www.deviantart.com/x"}

    async def _ensure():
        return _Client(), "tok"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_deviation_url", lambda *a, **kw: "https://www.deviantart.com/x")

    pkg = _package("txt", path=str(tmp_path / "s"))
    pkg.file_path = str(story)

    result = await p.edit("123", pkg)

    assert result.success is True
    assert sent.get("body") == "body text"
