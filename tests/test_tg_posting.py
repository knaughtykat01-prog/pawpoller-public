"""Telegram channel posting (gap-wave-6) — Posts-module broadcast target.

Locks the client's dispatch (text→sendMessage, 1 image→sendPhoto, N→sendMediaGroup),
channel normalisation, the public-URL rule, and the require-token/channel guard.
The network is faked — no real Telegram calls.
"""
import asyncio

import httpx
import pytest

from clients.tg.client import TgClient


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Records the (method, data) of each POST; returns a canned ok response.
    The response's result shape depends on the endpoint so media-group returns a
    list like the real API."""
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        _FakeAsyncClient.calls.append({"method": method, "data": data or {},
                                       "files": list((files or {}).keys())})
        if method == "sendMediaGroup":
            return _FakeResp({"ok": True, "result": [{"message_id": 55}, {"message_id": 56}]})
        return _FakeResp({"ok": True, "result": {"message_id": 42}})


@pytest.fixture(autouse=True)
def _fake_net(monkeypatch):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def _run(coro):
    return asyncio.run(coro)


def test_channel_normalisation():
    assert TgClient("t", "@chan").channel == "@chan"
    assert TgClient("t", "chan").channel == "@chan"
    assert TgClient("t", "https://t.me/chan").channel == "@chan"
    assert TgClient("t", "-1001234567890").channel == "-1001234567890"  # numeric id kept


def test_text_only_calls_send_message():
    c = TgClient("tok", "@chan")
    r = _run(c.create_post("hello world"))
    assert _FakeAsyncClient.calls[0]["method"] == "sendMessage"
    assert _FakeAsyncClient.calls[0]["data"]["chat_id"] == "@chan"
    assert r["id"] == "42"
    assert r["url"] == "https://t.me/chan/42"      # public @channel → t.me link


def test_single_image_calls_send_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")
    c = TgClient("tok", "@chan")
    r = _run(c.create_post("caption here", image_paths=[str(img)], spoiler=True))
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "sendPhoto"
    assert call["data"]["caption"] == "caption here"
    assert call["data"]["has_spoiler"] == "true"   # NSFW → spoiler blur
    assert "photo" in call["files"]
    assert r["id"] == "42"


def test_multi_image_calls_media_group(tmp_path):
    imgs = []
    for n in "abc":
        p = tmp_path / f"{n}.png"
        p.write_bytes(b"\x89PNG\r\n")
        imgs.append(str(p))
    c = TgClient("tok", "@chan")
    r = _run(c.create_post("album", image_paths=imgs))
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "sendMediaGroup"
    assert call["files"] == ["file0", "file1", "file2"]
    assert r["id"] == "55"                          # first message of the group


def test_numeric_channel_has_no_public_url():
    c = TgClient("tok", "-1001234567890")
    r = _run(c.create_post("hi"))
    assert r["url"] == ""                            # numeric id → no t.me link


def test_requires_token_and_channel():
    with pytest.raises(ValueError):
        _run(TgClient("", "@chan").create_post("x"))
    with pytest.raises(ValueError):
        _run(TgClient("tok", "").create_post("x"))


def test_publisher_lists_tg_supported():
    from posting import post_publisher
    assert "tg" in post_publisher.SUPPORTED
