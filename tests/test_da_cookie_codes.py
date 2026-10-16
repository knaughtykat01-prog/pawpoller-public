"""DA descriptions need the browser cookie; say so with a code, and catch it in Test (4.77.2).

A sync reported success while every description was quietly left alone: the account had no
da_cookie. Now the sync note, the account Test and the log all carry DA-COOKIE-MISSING or
DA-COOKIE-EXPIRED, and the DeviantArt guide says what to do for each.

Measured live 2026-10-15: a SIGNED-OUT deviantart.com page also has a csrfToken, and the
page's JSON arrives with escaped quotes — so "found a token" proved nothing, and the old
unescaped pattern found nothing at all.
"""
import asyncio

from fastapi.testclient import TestClient

from clients.da.client import DAClient

_OUT = r'"@@publicSession\":{\"isLoggedIn\":false,\"isMobile\":false,\"csrfToken\":\"tok-out.123\"}'
_IN = r'"@@publicSession\":{\"isLoggedIn\":true,\"isMobile\":false,\"csrfToken\":\"tok-in.456\"}'


def test_a_signed_out_page_is_not_a_session_even_with_a_token():
    assert DAClient._session_from_page(_OUT) == (False, "tok-out.123")
    assert DAClient._session_from_page(_IN) == (True, "tok-in.456")
    assert DAClient._session_from_page('{"isLoggedIn":true,"csrfToken":"plain"}') == (True, "plain")


def test_cookie_check_codes():
    assert asyncio.run(DAClient().check_cookie()) == (False, DAClient.ERR_COOKIE_MISSING)

    c = DAClient(cookie="userinfo=x")

    class _Resp:
        text = _OUT

        def raise_for_status(self):
            pass

    async def _get(*a, **k):
        return _Resp()

    c._http.get = _get
    assert asyncio.run(c.check_cookie()) == (False, DAClient.ERR_COOKIE_EXPIRED)
    _Resp.text = _IN
    assert asyncio.run(c.check_cookie()) == (True, "")


def test_a_deviantart_that_does_not_answer_is_not_an_expired_cookie():
    """REVIEW4772: a network error or a block page read as DA-COOKIE-EXPIRED."""
    import httpx
    c = DAClient(cookie="userinfo=x")
    for handler in (lambda r: httpx.Response(503, text="down"),
                    lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route", request=r))):
        c._http._transport = httpx.MockTransport(handler)
        assert asyncio.run(c.check_cookie()) == (False, DAClient.ERR_COOKIE_UNCHECKED)


def test_cookies_are_scoped_to_deviantart():
    c = DAClient(cookie="userinfo=x; auth=y")
    assert {(k.name, k.domain) for k in c._http.cookies.jar} == {("userinfo", ".deviantart.com"),
                                                                  ("auth", ".deviantart.com")}


def test_the_description_error_carries_its_code():
    try:
        asyncio.run(DAClient().napi_set_description("1", "text"))
    except RuntimeError as e:
        assert DAClient.ERR_COOKIE_MISSING in str(e)
    else:
        raise AssertionError("expected the missing-cookie error")


def test_account_test_warns_when_descriptions_cannot_sync(monkeypatch):
    import dashboard
    import config
    from database import accounts as adb
    from posting import manager

    monkeypatch.setattr(adb, "get_account", lambda conn, aid: {"platform": "da", "is_default": 1})
    monkeypatch.setattr(config, "resolve_account_credentials",
                        lambda p, aid, d: {"da_refresh_token": "r", "da_cookie": ""})

    class _Poster:
        async def validate_session(self):
            return {"ok": True, "logged_in": True, "matches": True, "username": "Inkwolf"}

    monkeypatch.setattr(manager, "_get_poster", lambda p, aid: _Poster())
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    r = c.post("/api/accounts/27/test-login").json()
    assert r["status"] == "partial" and r["code"] == DAClient.ERR_COOKIE_MISSING
    assert "descriptions" in r["detail"]


def test_the_guide_explains_each_code():
    src = open("frontend/js/platform_guides.js", encoding="utf-8").read()
    for code in (DAClient.ERR_COOKIE_MISSING, DAClient.ERR_COOKIE_EXPIRED):
        assert code in src, code
