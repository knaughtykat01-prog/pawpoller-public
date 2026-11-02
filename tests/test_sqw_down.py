"""SquidgeWorld being down is not a wrong password: login raises, so the session check shows
"error" (amber), not "expired" (red), and the poll doesn't say "check credentials"."""
import asyncio

import httpx
import pytest

from clients.sqw.client import SquidgeWorldClient

_FORM = ('<form action="/users/login"><input name="authenticity_token" value="tok">'
         '<input name="user[login]"></form>')


def _client(handler):
    c = SquidgeWorldClient("SecondFur", "pw", "SecondFur")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


def _run(c):
    return asyncio.run(c.validate_session())


def test_login_page_502_raises():
    c = _client(lambda req: httpx.Response(502))
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(c)


def test_login_post_5xx_raises():
    c = _client(lambda req: httpx.Response(200, text=_FORM) if req.method == "GET"
                else httpx.Response(503))
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(c)


def test_redirect_to_own_page_signs_in():
    def handler(req):
        if req.method == "POST":
            return httpx.Response(302, headers={"Location": "https://squidgeworld.org/users/SecondFur"})
        return httpx.Response(200, text=_FORM if req.url.path == "/users/login" else "<p>dashboard</p>")
    assert _run(_client(handler)) == "SecondFur"


def test_wrong_password_still_returns_none():
    c = _client(lambda req: httpx.Response(200, text=_FORM if req.method == "GET"
                                           else "<p>The password or user name you entered doesn't match</p>"))
    assert _run(c) is None
