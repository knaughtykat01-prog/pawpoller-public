"""FurryNetwork poster — post() flow with a faked client (no network)."""
import asyncio

import config
from posting.platforms.base import StoryUploadPackage
import posting.platforms.furrynetwork as fnp


class _FakeFn:
    def __init__(self, **kw):
        self.username = kw.get("username", "")
        self.password = kw.get("password", "")
        self.refresh_token = kw.get("refresh_token", "")
        self.access_token = kw.get("access_token", "")
        self.uploaded = None

    async def login(self):
        return True

    async def get_characters(self):
        return [{"name": "kit", "default": True}, {"name": "other"}]

    async def upload_artwork(self, **kw):
        self.uploaded = kw
        return {"success": True, "id": "555", "url": "https://furrynetwork.com/kit/artwork/555"}


def _pkg(**over):
    base = dict(story_name="Wolf", chapter_index=0, chapter_title="", platform="fn",
                title="Wolf", description="a wolf", tags=["wolf", "male"],
                rating="adult", file_path="/tmp/wolf.png")
    base.update(over)
    return StoryUploadPackage(**base)


def test_post_uploads_under_default_character(monkeypatch):
    config.save_settings({"fn_username": "me@ex.com", "fn_password": "pw"})
    monkeypatch.setattr(fnp, "FnClient", _FakeFn)
    poster = fnp.FurryNetworkPoster()
    res = asyncio.run(poster.post(_pkg()))
    assert res.success is True
    assert res.external_id == "555"
    assert "kit/artwork/555" in res.external_url
    up = poster._client.uploaded
    assert up["character"] == "kit"          # default character chosen
    assert up["title"] == "Wolf" and up["rating"] == "adult"
    assert up["tags"] == ["wolf", "male"]


def test_character_override(monkeypatch):
    config.save_settings({"fn_username": "me@ex.com", "fn_password": "pw"})
    monkeypatch.setattr(fnp, "FnClient", _FakeFn)
    poster = fnp.FurryNetworkPoster()
    res = asyncio.run(poster.post(_pkg(extra={"fn_character": "other"})))
    assert res.success is True
    assert poster._client.uploaded["character"] == "other"


def test_validate_requires_image_and_title():
    poster = fnp.FurryNetworkPoster()
    errs = poster.validate(_pkg(file_path="", title=""))
    assert any("image" in e.lower() for e in errs)
    assert any("title" in e.lower() for e in errs)
