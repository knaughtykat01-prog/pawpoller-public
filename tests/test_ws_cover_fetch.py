"""The Weasyl cover image fetch: no API key off-site, the proxy route kept, a size cap (WSPLAINPROXY).

4.56.2 (WSKEYHOST) moved the cover download off the keyed client, because its URL comes from the
page and the keyed client sends X-Weasyl-API-Key to whatever host it names. The release review
noted the plain client then skipped the CF-proxy transport the keyed client uses, and buffered
the whole body with no cap.
"""
from __future__ import annotations

import asyncio
import io

import httpx
import respx
from PIL import Image

from clients.weasyl import client as ws

PAGE = ('<form action="/manage/thumbnail"><input name="submitid" value="7"></form>'
        '<img id="imageselect" src="https://cdn.weasyl.com/cover.png">')


def _png(w=300, h=200) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h)).save(b, "PNG")
    return b.getvalue()


def _run(c):
    async def go():
        try:
            return await c.thumbnail_from_cover("7")
        finally:
            await c.close()
    return asyncio.run(go())


@respx.mock
def test_the_cover_is_measured_without_the_key_and_cropped_whole():
    respx.get("https://www.weasyl.com/manage/thumbnail?submitid=7").respond(200, text=PAGE)
    img = respx.get("https://cdn.weasyl.com/cover.png").respond(200, content=_png())
    crop = respx.post("https://www.weasyl.com/manage/thumbnail").respond(200, text="ok")
    assert _run(ws.WeasylClient(api_key="SECRET")) is True
    assert "x-weasyl-api-key" not in {k.lower() for k in img.calls[0].request.headers}
    body = crop.calls[0].request.content.decode()
    assert "x2=300" in body and "y2=200" in body


@respx.mock
def test_an_oversized_cover_is_not_read(monkeypatch):
    monkeypatch.setattr(ws, "_COVER_FETCH_CAP", 1024)
    respx.get("https://www.weasyl.com/manage/thumbnail?submitid=7").respond(200, text=PAGE)
    respx.get("https://cdn.weasyl.com/cover.png").respond(200, content=b"\0" * 5000)
    crop = respx.post("https://www.weasyl.com/manage/thumbnail").respond(200, text="ok")
    assert _run(ws.WeasylClient(api_key="SECRET")) is False
    assert crop.call_count == 0


def test_the_plain_client_takes_the_proxy_route(monkeypatch):
    used = []

    class _FakeProxy(httpx.AsyncBaseTransport):
        def __init__(self, url, key):
            used.append((url, key))

        async def handle_async_request(self, request):
            if request.url.host == "cdn.weasyl.com":
                return httpx.Response(200, content=_png(), request=request)
            if request.method == "GET":
                return httpx.Response(200, text=PAGE, request=request)
            return httpx.Response(200, text="ok", request=request)

    from polling import cf_proxy
    monkeypatch.setattr(cf_proxy, "CloudflareProxyTransport", _FakeProxy)
    c = ws.WeasylClient(api_key="SECRET", proxy_url="https://proxy.example", proxy_key="k")
    assert _run(c) is True
    # Once for the keyed client, once for the cover fetch.
    assert used == [("https://proxy.example", "k"), ("https://proxy.example", "k")]
