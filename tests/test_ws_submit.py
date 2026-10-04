"""Weasyl visual + literary submissions (4.54.1).

The first live post was refused: an unset category went as ``subtype=0``, which isn't one of
Weasyl's codes (1010–1999 art, 2010–2999 writing), and the client reported only "status 200"
because Weasyl answers a refusal by re-showing a page.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.weasyl import client as ws
from clients.weasyl.client import WeasylClient


class _Resp:
    status_code = 200

    def __init__(self, url, text=""):
        self.url, self.text = url, text


def _client(resp):
    calls = []

    class _HTTP:
        async def post(self, url, data=None, files=None, timeout=None, follow_redirects=False):
            calls.append({"url": url, "data": dict(data or {})})
            return resp

    c = WeasylClient.__new__(WeasylClient)
    c._http = _HTTP()

    async def _csrf(url):
        return ""
    c._get_csrf_token = _csrf
    return c, calls


@pytest.mark.parametrize("given,kind,expected", [
    (0, "visual", "1030"), (None, "visual", "1030"), ("junk", "visual", "1030"), (1020, "visual", "1020"),
    (2010, "visual", "1030"), (0, "literary", "2010"), (2020, "literary", "2020"), (1030, "literary", "2010"),
])
def test_only_weasyl_categories_are_ever_sent(tmp_path, given, kind, expected):
    f = tmp_path / ("a.png" if kind == "visual" else "a.txt")
    f.write_bytes(b"x")
    c, calls = _client(_Resp("https://www.weasyl.com/submission/77/a"))
    fn = c.submit_visual if kind == "visual" else c.submit_literary
    r = asyncio.run(fn(str(f), title="A", tags="a b", subtype=given))
    assert calls[0]["data"]["subtype"] == expected
    assert r == {"submission_id": "77", "url": "https://www.weasyl.com/submission/77/a"}


def test_todays_submission_address_counts_as_submitted(tmp_path):
    """Weasyl lands on /~user/submissions/N/slug (plural). Reading only /submission/N turned the
    first live post's success into a 'refusal' quoting the new page's own title, then retries."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    c, _ = _client(_Resp("https://www.weasyl.com/~sample/submissions/2345678/marker-muncher-pfp",
                         "<title>Marker Muncher (PFP) — Weasyl</title>"))
    assert asyncio.run(c.submit_visual(str(f), title="A", tags="a b"))["submission_id"] == "2345678"


def test_a_changed_key_rebuilds_the_cached_poster(monkeypatch):
    """The poster (and its client, holding the key) lived until a restart, so a replaced
    Weasyl key kept posting as the OLD account."""
    import config
    from posting import manager
    monkeypatch.setattr(manager, "_posters", {})
    monkeypatch.setattr(manager, "_poster_gen", {})
    monkeypatch.setattr(config, "_cred_generation", 5)
    first = manager._get_poster("ws", 999001)
    assert manager._get_poster("ws", 999001) is first          # unchanged key → same poster
    monkeypatch.setattr(config, "_cred_generation", 6)          # what save_settings does on a key change
    assert manager._get_poster("ws", 999001) is not first


def test_save_settings_bumps_the_generation_only_for_a_changed_credential(monkeypatch):
    import config
    store = {"ws_api_key": "old", "theme": "dark"}
    monkeypatch.setattr(config, "_load_settings", lambda: dict(store))
    monkeypatch.setattr(config, "_encrypt_vault", lambda creds: None)
    monkeypatch.setattr(config, "SETTINGS_PATH", __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "s.json")
    g = config.credentials_generation()
    config.save_settings({"theme": "light", "ws_api_key": "old"})
    assert config.credentials_generation() == g
    config.save_settings({"acct_3_ws_api_key": "new"})
    assert config.credentials_generation() == g + 1


def test_the_thumbnail_step_counts_as_submitted(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    c, _ = _client(_Resp("https://www.weasyl.com/manage/thumbnail?submitid=88"))
    assert asyncio.run(c.submit_visual(str(f), title="A", tags="a b"))["submission_id"] == "88"


def test_a_refusal_carries_weasyls_own_words(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    page = ('<html><head><title>Weasyl</title></head><body><div id="error_content" class="content">'
            '<p>You must select a valid  <b>subtype</b>.</p></div></body></html>')
    c, _ = _client(_Resp("https://www.weasyl.com/submit/visual", page))
    with pytest.raises(RuntimeError, match=r"refused the artwork: You must select a valid subtype\. \(status 200\)"):
        asyncio.run(c.submit_visual(str(f), title="A", tags="a b"))


def test_refusal_falls_back_to_the_title_then_to_nothing():
    assert ws._refusal("<title> Error -- Weasyl </title>") == "Error -- Weasyl"
    assert ws._refusal("plain") == ""
