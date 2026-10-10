"""AO3REMEMBER: a pasted AO3 sign-in renews itself.

AO3's `_otwarchive_session` lapses two weeks after the last visit and is re-issued on every logged-in
page; `remember_user_token` (Remember me) lasts three months. The client reports renewed cookies and
the poller stores them on the account they came from.
"""
import asyncio

import httpx

import config
from clients.ao3.client import AO3Client, REMEMBER_COOKIE, SESSION_COOKIE
from polling import ao3_poller


def _renewing_ao3(new_session: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<a>Log Out</a>", headers={
            "set-cookie": f"{SESSION_COOKIE}={new_session}; path=/; expires=Fri, 23 Oct 2026 00:00:00 GMT; secure; HttpOnly"})
    return httpx.MockTransport(handler)


def _visit(cli: AO3Client, new_session: str) -> None:
    cli._http._transport = _renewing_ao3(new_session)
    asyncio.run(cli._http.get("https://archiveofourown.org/users/kat"))


def _jar(cli: AO3Client, name: str) -> list[str]:
    return [c.value for c in cli._http.cookies.jar if c.name == name]


def test_sends_both_cookies_and_reports_the_renewed_session():
    cli = AO3Client("", "", "kat", session_cookie="old", remember_token="rem")
    assert cli._logged_in
    assert _jar(cli, SESSION_COOKIE) == ["old"] and _jar(cli, REMEMBER_COOKIE) == ["rem"]
    _visit(cli, "renewed")
    assert _jar(cli, SESSION_COOKIE) == ["renewed"]         # AO3's copy replaced ours, no duplicate
    assert cli.fresh_cookies() == {"ao3_session_cookie": "renewed"}
    assert cli.fresh_cookies() == {}                         # reported once


def test_password_sign_in_is_left_alone():
    cli = AO3Client("kat", "pw", "kat")
    _visit(cli, "server-made")
    assert cli.fresh_cookies() == {}


def test_moving_to_another_account_drops_the_first_ones_remember_token():
    cli = AO3Client("", "", "a", session_cookie="sa", remember_token="ra")
    cli.update_credentials("", "", "b", session_cookie="sb")
    assert _jar(cli, REMEMBER_COOKIE) == []
    assert _jar(cli, SESSION_COOKIE) == ["sb"] and cli._logged_in
    cli.update_credentials("", "", "c", remember_token="rc")
    assert _jar(cli, SESSION_COOKIE) == [] and _jar(cli, REMEMBER_COOKIE) == ["rc"]
    assert cli._logged_in                                    # the remember token alone signs in


def test_poller_stores_the_renewed_cookie_on_its_own_account(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    cli = AO3Client("", "", "kat", session_cookie="old")
    _visit(cli, "renewed")
    ao3_poller.save_fresh_cookies(cli, 7, False)
    assert saved == {config.account_setting_key(7, "ao3_session_cookie", False): "renewed"}
    saved.clear()
    ao3_poller.save_fresh_cookies(cli, 7, False)
    assert saved == {}


def test_remember_token_is_a_vaulted_account_field():
    assert "ao3_remember_token" in config.PLATFORM_CREDENTIAL_FIELDS["ao3"]
    assert config.is_credential_key("ao3_remember_token")
    assert config.is_credential_key("acct_7_ao3_remember_token")
