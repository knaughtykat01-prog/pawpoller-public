"""OTW Archive per-form CSRF tokens (4.78.1, AO3AUTHERR).

AO3 binds each form's authenticity_token to that form's action. With a site banner up, the first token on /works/new
belonged to the banner's dismiss form, so every new work POST came back on /auth_error. These pin that each flow
takes its own form's token, and that a page without the wanted form never falls back to a chrome form's token.
"""
import asyncio
from urllib.parse import parse_qs

import httpx

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
