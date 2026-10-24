"""IBWEB429: a refused Inkbunny web sign-in ends comment scraping for that poll.

A tester's log showed every later submission signing in again into the same 429, each ending in
AttributeError on None. Now one refusal, one attempt, and a plain error for the poller to log.
"""
import asyncio

import httpx
import pytest

from clients.ib.client import InkbunnyClient


def test_one_refusal_one_login_attempt(monkeypatch):
    calls = {"login_page": 0, "login_post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login.php"):
            calls["login_page"] += 1
            return httpx.Response(200, text="<input name='token' value='t'>")
        if request.url.path.endswith("/login_process.php"):
            calls["login_post"] += 1
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler),
                                                               follow_redirects=True))

    async def run():
        c = InkbunnyClient(username="Inkwolf", password="x")
        for sub in (1, 2, 3):
            with pytest.raises(RuntimeError, match="refused the web sign-in"):
                await c.scrape_comments(sub)

    asyncio.run(run())
    assert calls == {"login_page": 1, "login_post": 1}
