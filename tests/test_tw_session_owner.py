"""X posts as the account you chose, or not at all (4.6.3).

2026-09-04: three X account rows, one session. The first post that got past
error 226 went out "as" the second account and landed on the first — every
slot held the default account's cookies, and nothing asked X whose session
it was before writing. FA got that check in 3.31.0 (`validate_session`);
this is X's.

Contracts: `TWClient.session_owner()` reads the account-switcher list (the
v1.1 settings / verify_credentials endpoints are 404 for a cookie session);
the poster REFUSES BY NAME when the owner is known and is not this account,
posts with a warning when X will not say, and posts when they match; the
refusal is permanent (no three retries); the per-account Test reports
`wrong_account` with the real owner; browser login writes the chosen
account's keys, not the default slot (BLOGINACCT).
"""
from __future__ import annotations

import os
import tempfile

import httpx
import pytest

from clients.tw.client import TWClient

MULTI = {"users": [{"screen_name": "KnaughtyKat", "is_active": True}, {"screen_name": "SecondHandle"}]}


def _client(handler):
    c = TWClient("tok", "csrf", "target")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=c._http.headers)
    return c


class TestSessionOwner:
    @pytest.mark.asyncio
    async def test_reads_the_switcher_list_active_first_and_caches(self):
        calls = []

        async def h(req):
            calls.append(str(req.url))
            return httpx.Response(200, json=MULTI)
        c = _client(h)
        assert await c.session_owner() == ["KnaughtyKat", "SecondHandle"]
        assert await c.session_owner() == ["KnaughtyKat", "SecondHandle"]
        assert len(calls) == 1 and calls[0].endswith("/i/api/1.1/account/multi/list.json")

    @pytest.mark.asyncio
    async def test_unknown_is_empty_never_a_guess(self):
        async def h(req):
            return httpx.Response(404, json={"errors": [{"code": 34}]})
        assert await _client(h).session_owner() == []
        assert await TWClient("", "", "t").session_owner() == []

    @pytest.mark.asyncio
    async def test_new_credentials_forget_the_answer(self):
        async def h(req):
            return httpx.Response(200, json=MULTI)
        c = _client(h)
        await c.session_owner()
        c.update_credentials("tok2", "csrf2", "target")
        assert c._owners is None


# ── the poster ───────────────────────────────────────────────────────────────

def _package():
    from posting.platforms.base import StoryUploadPackage
    return StoryUploadPackage(story_name="Sample_Piece", chapter_index=0, chapter_title="", platform="tw",
                              title="Sample Piece", description="A quiet piece.", tags=["anthro"],
                              rating="general", file_path=__file__, file_type="png", word_count=0)


@pytest.fixture()
def poster(monkeypatch):
    from posting.platforms.twitter import TwitterPoster
    p = TwitterPoster()
    p.account_id = 14
    monkeypatch.setattr(TwitterPoster, "_creds", lambda self, settings=None: ("tok", "csrf", "target"))
    monkeypatch.setattr(TwitterPoster, "_account_handle", lambda self: "SecondHandle")
    calls = {"upload": 0, "tweet": 0}

    async def upload(self, path):
        calls["upload"] += 1
        return "555"

    async def tweet(self, text, media_ids=None, *, sensitive=False):
        calls["tweet"] += 1
        return {"id": "777", "url": "https://x.com/i/status/777"}
    monkeypatch.setattr(TWClient, "upload_media", upload)
    monkeypatch.setattr(TWClient, "create_tweet", tweet)
    return p, calls


class TestPosterRefuses:
    @pytest.mark.asyncio
    async def test_the_wrong_session_is_refused_by_name_before_anything_is_uploaded(self, poster, monkeypatch):
        p, calls = poster

        async def owner(self):
            return ["KnaughtyKat"]
        monkeypatch.setattr(TWClient, "session_owner", owner)
        r = await p.post(_package())
        assert r.success is False
        assert "@SecondHandle" in r.error and "@KnaughtyKat" in r.error and r.error.startswith("Not posted")
        assert calls == {"upload": 0, "tweet": 0}

    @pytest.mark.asyncio
    async def test_a_matching_session_posts(self, poster, monkeypatch):
        p, calls = poster

        async def owner(self):
            return ["secondhandle"]          # case does not matter
        monkeypatch.setattr(TWClient, "session_owner", owner)
        r = await p.post(_package())
        assert r.success is True and calls == {"upload": 1, "tweet": 1}

    @pytest.mark.asyncio
    async def test_an_unknown_owner_posts_with_a_warning_not_a_refusal(self, poster, monkeypatch, caplog):
        """If X retires the who-am-I endpoint too, refusing would silence every
        X post; the pre-4.6.3 behaviour with a warning is the honest fallback."""
        p, calls = poster

        async def owner(self):
            return []
        monkeypatch.setattr(TWClient, "session_owner", owner)
        with caplog.at_level("WARNING"):
            r = await p.post(_package())
        assert r.success is True and calls["tweet"] == 1
        assert any("could not verify whose session" in m for m in caplog.messages)

    @pytest.mark.asyncio
    async def test_no_account_row_means_no_check(self, poster, monkeypatch):
        p, calls = poster
        monkeypatch.setattr(type(p), "_account_handle", lambda self: "")

        async def owner(self):
            raise AssertionError("must not be asked when there is no handle to compare")
        monkeypatch.setattr(TWClient, "session_owner", owner)
        assert (await p.post(_package())).success is True

    def test_the_refusal_is_permanent_not_retried_three_times(self):
        from posting.manager import _PERMANENT_ERROR_MARKERS
        refusal = "Not posted: the X cookies stored for @SecondHandle belong to @KnaughtyKat."
        assert any(m in refusal.lower() for m in _PERMANENT_ERROR_MARKERS)


# ── the per-account Test and the browser login ───────────────────────────────

@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "pp.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


class TestAccountTest:
    @pytest.mark.asyncio
    async def test_reports_the_real_owner_as_wrong_account(self, conn, monkeypatch):
        import config
        from database import accounts
        from routes import settings_api
        aid = accounts.create_account(conn, "tw", "Second", handle="SecondHandle")
        conn.commit()
        monkeypatch.setattr(config, "resolve_account_credentials",
                            lambda plat, acct, is_def, settings=None: {"tw_auth_token": "a", "tw_ct0": "b"})

        async def owner(self):
            return ["KnaughtyKat"]
        monkeypatch.setattr(TWClient, "session_owner", owner)
        r = await settings_api.test_account_login(aid)
        assert r["status"] == "wrong_account" and r["username"] == "KnaughtyKat"
        assert "@SecondHandle" in r["detail"]

        async def owner2(self):
            return ["SecondHandle"]
        monkeypatch.setattr(TWClient, "session_owner", owner2)
        r = await settings_api.test_account_login(aid)
        assert r["status"] == "ok" and r["username"] == "SecondHandle"

        async def owner3(self):
            return []
        monkeypatch.setattr(TWClient, "session_owner", owner3)
        assert (await settings_api.test_account_login(aid))["status"] == "invalid"


class TestBrowserLoginSlot:
    def test_writes_the_chosen_accounts_keys_not_the_default_slot(self, conn, monkeypatch):
        import config
        from database import accounts
        from auth import browser_login
        aid = accounts.create_account(conn, "tw", "Second", handle="SecondHandle")
        conn.commit()
        saved = {}
        monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
        creds = {"tw_auth_token": "AAA", "tw_ct0": "CCC"}
        browser_login._save_browser_creds("tw", creds, aid)
        assert saved == {f"acct_{aid}_tw_auth_token": "AAA", f"acct_{aid}_tw_ct0": "CCC"}
        saved.clear()
        browser_login._save_browser_creds("tw", creds, None)
        assert saved == creds, "no account = the default slot, as before"
        with pytest.raises(ValueError):
            browser_login._save_browser_creds("fa", creds, aid)

    def test_the_route_and_the_page_carry_the_account(self):
        src = open("routes/settings_api.py", encoding="utf-8").read()
        assert "account_id: int | None = None" in src and "account_id=account_id" in src
        api = open("frontend/js/api.js", encoding="utf-8").read()
        assert "browserLogin(platform, extraFields = {}, accountId = null)" in api
        page = open("frontend/js/accounts.js", encoding="utf-8").read()
        assert "data-cred-browser" in page and "API.browserLogin(platform, {}, Number(accountId))" in page
