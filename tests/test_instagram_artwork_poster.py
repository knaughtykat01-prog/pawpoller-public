"""Instagram artwork poster (2.139.0) — validation, caption build, happy path.

The poster reuses the Posts module's public-image-hosting path; here the stash +
IgClient are mocked so there's no network or real hosting. Mirrors the e621
poster test shape.
"""
import asyncio

import config
from posting.platforms.instagram import InstagramPoster, _build_caption, _hashtags
from posting.platforms.base import StoryUploadPackage


def _pkg(**kw):
    d = dict(story_name="Art", chapter_index=0, chapter_title="", platform="ig",
             title="My Art", description="A scene.", tags=["fox", "vore", "soft", "maw"],
             rating="adult", file_path="/tmp/x.png")
    d.update(kw)
    return StoryUploadPackage(**d)


# ── Caption + hashtags ───────────────────────────────────────

def test_hashtags_sanitise_dedupe_cap():
    assert _hashtags(["fox pred", "fox pred", "soft_vore", "maw!!", "", "a"]) \
        == "#foxpred #soft_vore #maw #a"
    many = [f"t{i}" for i in range(40)]
    assert len(_hashtags(many).split()) == 30      # capped at IG's 30-hashtag ceiling


def test_caption_body_plus_hashtags():
    assert _build_caption(_pkg()) == "A scene.\n\n#fox #vore #soft #maw"
    assert _build_caption(_pkg(description="", title="Just A Title")).startswith("Just A Title")


# ── Validation ───────────────────────────────────────────────

def test_validate_requires_image(upload_file):
    config.save_settings({"ig_public_base_url": "https://pp.example"})
    p = InstagramPoster()
    assert any("image file" in e for e in p.validate(_pkg(file_path="")))
    assert p.validate(_pkg(file_path=upload_file)) == []


def test_validate_requires_public_host(upload_file):
    config.save_settings({"ig_public_base_url": "", "posting_server_url": ""})
    p = InstagramPoster()
    assert any("public image host" in e for e in p.validate(_pkg(file_path=upload_file)))


# ── Post ─────────────────────────────────────────────────────

def test_post_not_connected():
    config.save_settings({"ig_public_base_url": "https://pp.example",
                          "ig_access_token": "", "ig_user_id": ""})
    res = asyncio.run(InstagramPoster().post(_pkg()))
    assert res.success is False and "connected" in (res.error or "")


def test_post_happy_path(monkeypatch, upload_file):
    config.save_settings({"ig_public_base_url": "https://pp.example",
                          "ig_access_token": "TOKEN", "ig_user_id": "42"})
    from posting import ig_media
    monkeypatch.setattr(ig_media, "stash_image", lambda path: "a" * 32)
    monkeypatch.setattr(ig_media, "cleanup", lambda tok: None)

    captured = {}

    class FakeIg:
        def __init__(self, access_token="", user_id=""):
            captured["token"] = access_token
            captured["user"] = user_id

        async def create_post(self, caption, image_urls):
            captured["caption"] = caption
            captured["urls"] = image_urls
            return {"id": "MEDIA1", "url": "https://instagram.com/p/xyz/"}

        async def close(self):
            pass

    monkeypatch.setattr("clients.ig.client.IgClient", FakeIg)

    res = asyncio.run(InstagramPoster().post(_pkg(file_path=upload_file)))
    assert res.success is True
    assert res.external_id == "MEDIA1"
    assert res.external_url == "https://instagram.com/p/xyz/"
    assert captured["token"] == "TOKEN" and captured["user"] == "42"
    assert captured["urls"] == ["https://pp.example/api/ig/pubmedia/" + "a" * 32 + ".jpg"]
    assert "#fox" in captured["caption"]
