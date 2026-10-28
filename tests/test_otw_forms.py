"""OTW Archive per-form CSRF tokens (4.78.1, AO3AUTHERR).

AO3 binds each form's authenticity_token to that form's action. With a site banner up, the first token on /works/new
belonged to the banner's dismiss form, so every new work POST came back on /auth_error. These pin that each flow
takes its own form's token, and that a page without the wanted form never falls back to a chrome form's token.
"""
import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from clients.ao3.client import AO3Client
from clients.otw_forms import otw_form


def _form(action, token, extra=""):
    return (f'<form action="{action}" accept-charset="UTF-8" method="post">'
            f'<input type="hidden" name="authenticity_token" value="{token}" autocomplete="off" />{extra}</form>')


CHROME = (_form("/users/SecondFur/end_banner", "BANNER")
          + '<form class="search" action="/works/search" method="get"><input name="q"></form>')
SKINS = _form("/skins/unset", "SKIN0") + _form("/skins/891/set", "SKIN1")
NEW_WORK = (CHROME + _form("/works", "WORK",
                           '<input type="checkbox" value="77" checked="checked" '
                           'name="work[author_attributes][ids][]" />') + SKINS)


def _token(page, action):
    import re
    return re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', otw_form(page, action)).group(1)


def test_each_action_gets_its_own_forms_token():
    assert _token(NEW_WORK, r"/works") == "WORK"
    delete = CHROME + _form("/works/123", "DEL", '<input name="_method" value="delete">') + SKINS
    assert _token(delete, r"/works/\d+") == "DEL"
    chapter = CHROME + _form("/works/123/chapters/456/post", "POSTCH") + SKINS
    assert _token(chapter, r"/works/\d+/chapters/\d+/post") == "POSTCH"
    assert _token(CHROME + _form("https://archiveofourown.org/skins/9?x=1", "ABS"), r"/skins/\d+") == "ABS"


def test_no_matching_form_never_falls_back_to_a_chrome_token():
    page = CHROME + '<input type="hidden" name="authenticity_token" value="LOOSE" />' + SKINS
    assert _token(page, r"/works") == "LOOSE"


def test_create_work_posts_the_work_forms_token():
    sent = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, text=NEW_WORK)
        sent.update(parse_qs(req.content.decode()))
        return httpx.Response(302, headers={"location": "https://archiveofourown.org/works/999"})

    cli = AO3Client("", "", "SecondFur", session_cookie="s")
    cli._http._transport = httpx.MockTransport(lambda r: handler(r) if "/works/999" not in str(r.url)
                                               else httpx.Response(200, text="ok"))

    async def _no_delay():
        return None
    cli._polite_delay = _no_delay
    out = asyncio.run(cli.create_work(title="Sample Story", content="<p>Ten or more characters.</p>"))
    assert sent["authenticity_token"] == ["WORK"] and out["work_id"] == "999"


def test_ao3_validation_errors_are_named_and_not_retried(monkeypatch, tmp_path):
    page = ('<div id="error" class="error"><h4>Sorry! We couldn&#39;t save this work because:</h4><ul>'
            "<li>Title can&#39;t be blank</li><li>Content must be at least 10 characters long.</li></ul></div>")

    def handler(req):
        return httpx.Response(200, text=NEW_WORK if req.method == "GET" else page)
    cli = AO3Client("", "", "SecondFur", session_cookie="s")
    cli._http._transport = httpx.MockTransport(handler)

    async def _no_delay():
        return None
    cli._polite_delay = _no_delay
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    try:
        asyncio.run(cli.create_work(title="", content=""))
    except RuntimeError as e:
        msg = str(e)
    assert msg == "AO3 didn't save the work: Title can't be blank; Content must be at least 10 characters long."
    from posting.manager import _PERMANENT_ERROR_MARKERS
    assert any(m in msg.lower() for m in _PERMANENT_ERROR_MARKERS)


def test_an_ao3_busy_message_is_retried(monkeypatch, tmp_path):
    """SECLOW4781 (3): only a real validation error is permanent."""
    page = ('<div id="error" class="error"><h4>Sorry!</h4><ul>'
            "<li>We are experiencing heavy load, please try again later.</li></ul></div>")
    cli = AO3Client("", "", "SecondFur", session_cookie="s")
    cli._http._transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text=NEW_WORK if req.method == "GET" else page))

    async def _no_delay():
        return None
    cli._polite_delay = _no_delay
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError) as e:
        asyncio.run(cli.create_work(title="t", content="c" * 20))
    from posting.manager import _PERMANENT_ERROR_MARKERS
    assert "try again later" in str(e.value)
    assert not any(m in str(e.value).lower() for m in _PERMANENT_ERROR_MARKERS)


def test_a_hostile_page_stays_fast():
    """SECLOW4781: the old page-wide regex took ~50 s on 360 KB of unclosed form tags."""
    import time
    for page in ('<form action="/x">' * 20000, "<form a" * 20000, "<form " + ' action="a' * 20000 + ">"):
        t = time.perf_counter()
        otw_form(page, r"/works")
        assert time.perf_counter() - t < 1.0


def test_data_action_is_not_the_action():
    page = '<form data-action="/works" action="/users/SecondFur/end_banner">'            '<input name="authenticity_token" value="BANNER"></form>' + _form("/works", "WORK")
    assert _token(page, r"/works") == "WORK"


def test_ao3_error_fallback_reads_error_texts_and_stays_fast():
    """SECLOW4790: the create_work fallback regex took ~26 s on unclosed class="error"> tags."""
    import time
    from clients.ao3.client import _error_texts
    page = '<p class="flash error">Title is too long</p><div class="notice">ok</div><span class="error"></span>'
    assert _error_texts(page) == ["Title is too long"]
    for hostile in ('class="error">' * 14000, 'class="error ' + "error " * 40000, 'class="x' * 30000):
        t = time.perf_counter()
        _error_texts(hostile)
        assert time.perf_counter() - t < 1.0
