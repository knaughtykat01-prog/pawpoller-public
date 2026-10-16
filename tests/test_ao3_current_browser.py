"""AO3 connects like a current browser (spec 041).

AO3's Cloudflare began challenging connections that present as an outdated Chrome; PawPoller's hard-coded Chrome 131
headers over plain httpx were exactly that. The direct client now rides curl_cffi's current-Chrome transport and adds
no headers of its own. These pin: which transport and headers each path gets, the fallback when curl is missing, that
both frozen builds bundle it, and that the AO3 Test answers inside the hosted seat's 100 s front door.
"""
import asyncio
import logging
import re
from pathlib import Path

import httpx
import pytest

from clients.ao3 import client as ao3
from clients.ao3.client import AO3Client, _HEADERS
from polling import account_probe as ap


def test_the_package_ships_in_this_install():
    """Runs inside the release image too: a build without the wheel fails `release.py check`."""
    from httpx_curl_cffi import AsyncCurlTransport  # noqa: F401
    assert ao3.AsyncCurlTransport is not None


def test_direct_client_uses_current_chrome_and_no_headers_of_its_own():
    cli = AO3Client("", "", "SecondFur", session_cookie="s")
    assert isinstance(cli._http._transport, ao3.AsyncCurlTransport)
    assert cli._headers == {}
    assert "Chrome/131" not in str(cli._http.headers)          # the old pinned identity is gone from this path
    asyncio.run(cli.close())


def test_no_browser_version_is_pinned_on_the_direct_path():
    src = Path(ao3.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _direct_transport"):src.index("SESSION_COOKIE = ")]
    assert 'impersonate="chrome"' in body and not re.search(r'impersonate="chrome\d', body)


def test_missing_curl_falls_back_and_says_so_once(monkeypatch, caplog):
    monkeypatch.setattr(ao3, "AsyncCurlTransport", None)
    monkeypatch.setattr(ao3, "_warned_no_curl", False)
    with caplog.at_level(logging.WARNING, logger=ao3.logger.name):
        a = AO3Client("", "", "SecondFur")
        b = AO3Client("", "", "SecondFur")
    assert isinstance(a._http._transport, httpx.AsyncHTTPTransport) and a._headers is _HEADERS
    assert sum("curl_cffi" in r.getMessage() for r in caplog.records) == 1
    for c in (a, b):
        asyncio.run(c.close())


def test_relay_path_is_unchanged():
    from polling.cf_proxy import CloudflareProxyTransport
    cli = AO3Client("", "", "SecondFur", proxy_url="https://relay.example", proxy_key="k")
    assert isinstance(cli._http._transport, CloudflareProxyTransport) and cli._headers is _HEADERS
    asyncio.run(cli.close())


@pytest.mark.repo_only      # the server image leaves the .spec files out (.dockerignore)
def test_both_frozen_builds_bundle_it():
    for spec in ("pawpoller.spec", "pawpoller-server.spec"):
        text = Path(spec).read_text(encoding="utf-8")
        assert "'httpx_curl_cffi'" in text and "'curl_cffi'" in text, spec


# ── The Test button ──────────────────────────────────────────

def test_a_page_ao3_never_sent_is_not_logged_in(monkeypatch):
    cli = AO3Client("", "", "SecondFur", session_cookie="s")

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(cli, "_get_page", _none)
    with pytest.raises(RuntimeError, match="isn't responding"):
        asyncio.run(cli.validate_session(strict=True))
    assert asyncio.run(cli.validate_session()) == "SecondFur"     # connect keeps trusting a just-pasted cookie
    asyncio.run(cli.close())


def test_a_hanging_ao3_test_answers_inside_the_cap(monkeypatch):
    class AO3Client:
        def __init__(self, **kw):
            pass

        async def validate_session(self, **kw):
            await asyncio.sleep(30)

        async def close(self):
            pass
    monkeypatch.setattr("clients.ao3.client.AO3Client", AO3Client)
    monkeypatch.setitem(ap._TEST_CAP, "ao3", 0.2)
    v = asyncio.run(ap.probe_account("ao3", {"ao3_session_cookie": "s"}, 1, True, {}))
    assert v["status"] == "error" and "AO3 isn't responding" in v["detail"]


def test_ao3_cap_is_well_under_cloudflares_100_seconds():
    assert ap._TEST_CAP["ao3"] <= 60
