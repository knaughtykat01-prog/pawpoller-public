"""FurAffinity audio — MEDIATYPES phase 2, site 5 (4.19.2).

FA's third upload kind is ``music`` (mp3 / wav, ≤ 10 MB), filed under category 16 with a custom
thumbnail — the Library's poster. Its only video is Flash, which the Library does not hold, so a
video is refused before the network. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

from posting.platforms.base import StoryUploadPackage
from posting.platforms.furaffinity import FurAffinityPoster, _is_audio


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def submit_story(self, file_path, **kw):
        self.calls.append({"method": "submit_story", "file": file_path, **kw})
        return {"submission_id": "66103446", "url": "https://www.furaffinity.net/view/66103446/"}

    async def submit_visual(self, file_path, **kw):
        self.calls.append({"method": "submit_visual", "file": file_path, **kw})
        return {"submission_id": "1", "url": "https://www.furaffinity.net/view/1/"}


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Track", chapter_index=0, chapter_title="", platform="fa",
              title="Sample Track", description="a tune", tags=["a", "b", "c"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(fake, monkeypatch):
    import config
    p = FurAffinityPoster()

    async def _ensure():
        return fake
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(config, "get_settings", lambda: {"artwork_fa_species": "3", "artwork_fa_gender": "2"})
    return p


def test_an_mp3_is_a_music_submission_in_category_16_with_the_poster(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster = tmp_path / "poster.png"
    poster.write_bytes(b"\x89PNG")
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "mp3", "audio", thumbnail_path=str(poster))))
    assert r.success and r.external_id == "66103446"
    call = fake.calls[0]
    assert call["method"] == "submit_story" and call["submission_type"] == "music"
    assert call["cat"] == "16" and call["thumbnail_path"] == str(poster)
    assert call["species"] == "3" and call["gender"] == "2"          # the artwork defaults, like a picture
    assert call["title"] == "Sample Track" and call["keywords"] == "a b c"


def test_the_piece_can_override_the_category(tmp_path, monkeypatch):
    track = tmp_path / "track.wav"
    track.write_bytes(b"RIFF")
    fake = _FakeClient()
    asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "wav", "audio", extra={"cat": "17"})))
    assert fake.calls[0]["submission_type"] == "music" and fake.calls[0]["cat"] == "17"


def test_pictures_and_stories_are_unchanged(tmp_path, monkeypatch):
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    fake = _FakeClient()
    p = _poster(fake, monkeypatch)
    asyncio.run(p.post(_pkg(pic, "png", "image")))
    assert fake.calls[0]["method"] == "submit_visual"
    doc = tmp_path / "story.txt"
    doc.write_text("once")
    asyncio.run(p.post(_pkg(doc, "txt", "")))
    assert fake.calls[1]["method"] == "submit_story" and fake.calls[1].get("submission_type", "story") == "story" and fake.calls[1]["cat"] == "13"


def test_is_audio_by_library_kind_or_extension():
    assert _is_audio(_pkg("t.mp3", "mp3", "")) and _is_audio(_pkg("t.wav", "wav", "audio"))
    assert not _is_audio(_pkg("p.png", "png", "image")) and not _is_audio(_pkg("s.txt", "txt", ""))


def test_video_and_flac_refused_before_the_network_mp3_wav_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("fa")
    for name, ft, kind in (("c.mp4", "mp4", "video"), ("t.flac", "flac", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        r = p.media_refusal(_pkg(f, ft, kind))
        assert r and r.startswith(f"FurAffinity doesn't take {ft} {kind}") and "mp3, wav audio" in r
    for name, ft in (("t.mp3", "mp3"), ("t.wav", "wav")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, "audio")) is None


def test_the_10_mb_cap_covers_audio(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(FurAffinityPoster, "max_file_size", 100)
    errs = FurAffinityPoster().validate(_pkg(track, "mp3", "audio"))
    assert any("too large" in e.lower() or "MB" in e for e in errs), errs
