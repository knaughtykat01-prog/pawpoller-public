"""FA cookie validation must be capable of failing (3.19.1).

The previous check could not:

```python
return "<figure" in resp.text or f"gallery/{self.username}" in str(resp.url)
```

A FurAffinity gallery is **public** — it serves `<figure>` thumbnails to anyone,
logged in or not — and the second clause is true for any successful fetch of
that URL. So it returned True for a logged-OUT session, while its docstring
claimed "if cookies are expired… no <figure> elements".

Measured on the production server against genuinely expired cookies: gallery
`200`, `<figure>` present, **no logout link**, page offering "log in" — and
`validate_cookies()` returned **True**.

⚠ The cost was a three-layer disguise. Expired cookies passed validation; the
post then died on "Could not find form key on /submit/"; and
`requires_mode = "desktop"` re-queued it for the desktop — so an expired login
reached the user as a *platform limitation* ("FA needs the desktop"). The one
check that existed to catch it was structurally incapable of reporting failure,
so nothing ever contradicted the story.
"""
from __future__ import annotations

import pytest

# A gallery page as FA really serves it to a LOGGED-OUT visitor: thumbnails
# present (it is public), no logout control, a login prompt in the header.
LOGGED_OUT_GALLERY = """
<html><head><title>Gallery</title></head><body>
<a href="/login/">Log in</a> | <a href="/register/">Register</a>
<section class="gallery">
  <figure id="sid-1"><img src="/thumb1.jpg"></figure>
  <figure id="sid-2"><img src="/thumb2.jpg"></figure>
</section>
</body></html>
"""

LOGGED_IN_CONTROLS = """
<html><head><title>Submissions</title></head><body>
<nav><a href="/controls/">Controls</a><a href="/logout/?key=abc123">Log Out</a></nav>
<div id="submissions"></div>
</body></html>
"""

LOGGED_OUT_CONTROLS = """
<html><head><title>Fur Affinity</title></head><body>
<form action="/login/"><input name="name"><input name="pass" type="password"></form>
<p>You must log in to view this page.</p>
</body></html>
"""


class _Resp:
    def __init__(self, text, status=200, url="https://www.furaffinity.net/x"):
        self.text, self.status_code, self.url = text, status, url


def _client(monkeypatch, page, status=200):
    from clients.fa.client import FAClient

    c = FAClient(username="SecondFur", cookie_a="a", cookie_b="b")

    class _Http:
        async def get(self, url, **kw):
            return _Resp(page, status, url)

    async def _fake(): return _Http()
    monkeypatch.setattr(c, "_get_fa_http", _fake)
    return c


# ── the regression ───────────────────────────────────────────────

def test_a_logged_out_session_is_rejected(monkeypatch):
    """The exact failure seen in production. Under the old check this returned
    True, because the page it trusted is public."""
    import asyncio
    c = _client(monkeypatch, LOGGED_OUT_CONTROLS)
    assert asyncio.run(c.validate_cookies()) is False


def test_a_public_gallery_full_of_figures_proves_nothing(monkeypatch):
    """`<figure>` was the old signal. It is present on a logged-out page, so it
    cannot distinguish a valid session from an expired one."""
    import asyncio
    c = _client(monkeypatch, LOGGED_OUT_GALLERY)
    assert "<figure" in LOGGED_OUT_GALLERY          # the old signal is there…
    assert asyncio.run(c.validate_cookies()) is False   # …and means nothing


def test_a_logged_in_session_is_accepted(monkeypatch):
    import asyncio
    c = _client(monkeypatch, LOGGED_IN_CONTROLS)
    assert asyncio.run(c.validate_cookies()) is True


def test_it_asks_for_a_page_behind_auth(monkeypatch):
    """A public page cannot answer "am I logged in?", whatever you look for on
    it. The request itself has to be one only a session can satisfy."""
    import asyncio
    from clients.fa.client import FAClient
    seen = []
    c = FAClient(username="SecondFur", cookie_a="a", cookie_b="b")

    class _Http:
        async def get(self, url, **kw):
            seen.append(url)
            return _Resp(LOGGED_IN_CONTROLS)

    async def _fake(): return _Http()
    monkeypatch.setattr(c, "_get_fa_http", _fake)
    asyncio.run(c.validate_cookies())
    assert any("/controls/" in u for u in seen), f"asked for {seen}"
    assert not any("/gallery/" in u for u in seen), "a public gallery cannot answer this"


# ── fails closed ─────────────────────────────────────────────────

def test_missing_cookies_short_circuit(monkeypatch):
    import asyncio
    from clients.fa.client import FAClient
    for a, b, u in [("", "b", "n"), ("a", "", "n"), ("a", "b", "")]:
        c = FAClient(username=u, cookie_a=a, cookie_b=b)
        assert asyncio.run(c.validate_cookies()) is False


def test_a_non_200_is_invalid(monkeypatch):
    import asyncio
    c = _client(monkeypatch, LOGGED_IN_CONTROLS, status=403)
    assert asyncio.run(c.validate_cookies()) is False


def test_a_network_error_is_invalid_not_an_exception(monkeypatch):
    """Fails closed: a false negative costs a retry, a false positive costs a
    silent misattributed failure."""
    import asyncio
    from clients.fa.client import FAClient
    c = FAClient(username="u", cookie_a="a", cookie_b="b")

    class _Http:
        async def get(self, url, **kw):
            raise RuntimeError("network down")

    async def _fake(): return _Http()
    monkeypatch.setattr(c, "_get_fa_http", _fake)
    assert asyncio.run(c.validate_cookies()) is False


def test_the_old_signal_is_gone_from_the_implementation():
    """Guard against a future 'optimisation' back to the public page."""
    import inspect
    from clients.fa.client import FAClient
    src = inspect.getsource(FAClient.validate_cookies)
    body = src.split('"""')[-1]          # drop the docstring, which quotes it
    assert "<figure" not in body
    assert "/gallery/" not in body


# ── the downstream message names the real cause ──────────────────

def test_a_logged_out_submit_page_reports_expired_cookies():
    """"Could not find form key" described the symptom and sent a real expired
    session off to be diagnosed as a scraping or platform problem."""
    import inspect
    from clients.fa import client as fac
    src = inspect.getsource(fac)
    assert "not logged in — the session cookies (a/b) are expired" in src
