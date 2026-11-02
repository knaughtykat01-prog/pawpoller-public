"""AO3 being down is not a wrong password (AO3DOWN): login sets blocked_reason, so the session
check shows amber, not red "expired", and the poll doesn't say "check credentials"."""
import asyncio

import httpx
import pytest

from clients.ao3 import client as ao3

_FORM = ('<form action="/users/login"><input name="authenticity_token" value="tok">'
         '<input name="user[login]"></form>')


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _sleep(_s):
        return None
    monkeypatch.setattr(ao3.asyncio, "sleep", _sleep)


def _client(handler):
    c = ao3.AO3Client("SecondFur", "pw", "SecondFur")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


def _run(c):
    return asyncio.run(c.validate_session())


@pytest.mark.parametrize("status", [502, 503])
def test_login_page_5xx_is_down(status):
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(_client(lambda req: httpx.Response(status)))


def test_no_answer_is_down():
    def handler(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(_client(handler))


def test_maintenance_page_is_down():
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(_client(lambda req: httpx.Response(200, text="<h1>The Archive is down for maintenance</h1>")))


def test_login_post_5xx_is_down():
    c = _client(lambda req: httpx.Response(200, text=_FORM) if req.method == "GET" else httpx.Response(503))
    with pytest.raises(RuntimeError, match="isn't responding"):
        _run(c)


def test_rerendered_login_page_is_not_signed_in():
    """A failed login lands back on /users/login; that URL holds "/users/" but is not a sign-in."""
    c = _client(lambda req: httpx.Response(200, text=_FORM if req.method == "GET"
                                           else "<p>The password or user name you entered doesn't match</p>"))
    assert _run(c) is None


def test_redirect_to_own_page_signs_in():
    def handler(req):
        if req.method == "POST":
            return httpx.Response(302, headers={"Location": "https://archiveofourown.org/users/SecondFur"})
        return httpx.Response(200, text=_FORM if req.url.path == "/users/login" else "<p>dashboard</p>")
    assert _run(_client(handler)) == "SecondFur"
