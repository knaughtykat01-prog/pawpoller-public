"""TWSTATUS, PLATAUDIT and TGBROADCAST -- the three rows left open after 4.34.1.

Each closes a different shape of "the app knows, and the screen says otherwise".
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from polling import session_check


def _run(coro):
    return asyncio.run(coro)


class _FakeTw:
    """Stands in for TWClient. Only the two calls the check makes."""

    def __init__(self, alive=True, owners=None):
        self._alive, self._owners = alive, list(owners or [])

    async def validate_cookies(self):
        return self._alive

    async def session_owner(self):
        return list(self._owners)


@pytest.fixture
def tw(monkeypatch):
    """Point session_check's tw branch at a fake client."""
    holder = {}

    def _factory(settings, auth, ct0, target):
        return holder["client"]

    import polling.tw_poller as twp
    monkeypatch.setattr(twp, "_get_or_create_client", _factory)
    return holder


COOKIES = {"tw_auth_token": "a", "tw_ct0": "c", "tw_target_user": "TheOwner"}


class TestXSessionCheckReportsWhoseSessionItIs:
    """TWSTATUS phase 4 (spec status_and_sort.md 1.4 step 2). Phase 1 made the row
    read a verdict; X had no verdict to read, so it sat on "saved -- not verified".

    The check had to be identity-reporting, not merely alive. `validate_cookies`
    proves SOME backend can authenticate. It cannot prove whose account a post would
    go out as -- and on 2026-09-04 a post "as" the second of three account rows
    landed on the first, because there was only ever one session.
    """

    def test_x_is_now_checkable_and_labelled(self):
        assert "tw" in session_check.CHECKABLE
        assert session_check.LABELS["tw"] == "X/Twitter"

    def test_cookies_alone_count_as_configured(self):
        assert session_check._configured("tw", dict(COOKIES))

    def test_a_bearer_token_alone_counts(self):
        """The official API needs no cookies; routes/api.py gates on cookies only."""
        assert session_check._configured(
            "tw", {"tw_api_bearer_token": "t", "tw_polling_backend": "auto"})

    def test_a_bearer_token_does_not_count_when_that_backend_is_off(self):
        """Otherwise picking 'graphql' with a stale token looks configured forever."""
        assert not session_check._configured(
            "tw", {"tw_api_bearer_token": "t", "tw_polling_backend": "graphql"})

    def test_nothing_configured_is_not_configured(self):
        assert not session_check._configured("tw", {})

    def test_dead_cookies_fail(self, tw):
        tw["client"] = _FakeTw(alive=False)
        assert _run(session_check._validate("tw", dict(COOKIES))) is False

    def test_the_right_owner_passes(self, tw):
        tw["client"] = _FakeTw(owners=["TheOwner"])
        assert _run(session_check._validate("tw", dict(COOKIES))) is True

    def test_the_owner_match_ignores_case(self, tw):
        tw["client"] = _FakeTw(owners=["theowner"])
        assert _run(session_check._validate("tw", dict(COOKIES))) is True

    def test_a_second_account_on_the_switcher_still_counts(self, tw):
        """session_owner returns every account the browser had, active first."""
        tw["client"] = _FakeTw(owners=["SomeoneElse", "TheOwner"])
        assert _run(session_check._validate("tw", dict(COOKIES))) is True

    def test_the_wrong_owner_fails_and_names_both(self, tw):
        """The whole point: a live session belonging to someone else is not success."""
        tw["client"] = _FakeTw(owners=["SomeoneElse"])
        v = _run(session_check._validate("tw", dict(COOKIES)))
        assert isinstance(v, dict) and v["ok"] is False
        assert "SomeoneElse" in v["detail"], "must name who it IS"
        assert "TheOwner" in v["detail"], "and who it should be"

    def test_an_unknown_owner_is_not_reported_as_success(self, tw):
        """Cookies that authenticate while X declines to say whose they are is exactly
        the DeviantArt bug. It must land as 'could not verify', not a green dot."""
        tw["client"] = _FakeTw(owners=[])
        with pytest.raises(RuntimeError):
            _run(session_check._validate("tw", dict(COOKIES)))

    def test_a_bearer_only_install_has_no_owner_to_be_wrong_about(self, tw):
        """Polling works and posting is not configured, so unknown ownership is not a
        hazard -- raising there would cry wolf at a correctly-set-up install."""
        tw["client"] = _FakeTw(owners=[])
        assert _run(session_check._validate(
            "tw", {"tw_api_bearer_token": "t", "tw_target_user": "TheOwner"})) is True


class TestACheckCanExplainItsOwnVerdict:
    """The reason a tw check fails is RUNTIME data -- which account the session
    actually belongs to -- so the per-code constant in _EXPIRED_DETAIL cannot carry
    it. A dict verdict can; a bool still takes the constant.
    """

    def test_a_dict_verdict_supplies_the_detail(self, tw, monkeypatch):
        tw["client"] = _FakeTw(owners=["SomeoneElse"])
        monkeypatch.setattr(session_check.config, "get_settings", lambda: dict(COOKIES))
        e = _run(session_check.check_platform("tw", dict(COOKIES)))
        assert e["status"] == "expired"
        assert "SomeoneElse" in e["detail"]

    def test_a_bool_verdict_still_takes_the_constant(self, tw, monkeypatch):
        tw["client"] = _FakeTw(alive=False)
        monkeypatch.setattr(session_check.config, "get_settings", lambda: dict(COOKIES))
        e = _run(session_check.check_platform("tw", dict(COOKIES)))
        assert e["detail"] == session_check._DEFAULT_EXPIRED_DETAIL

    def test_an_unknown_owner_lands_amber_not_red(self, tw, monkeypatch):
        """'error' means could-not-verify; 'expired' would send them to re-enter
        cookies that are working."""
        tw["client"] = _FakeTw(owners=[])
        monkeypatch.setattr(session_check.config, "get_settings", lambda: dict(COOKIES))
        e = _run(session_check.check_platform("tw", dict(COOKIES)))
        assert e["status"] == "error"

    def test_the_settings_row_shows_the_reason_not_the_constant(self):
        """A detail naming the wrong account is useless if the row overwrites it with
        'Credentials expired -- re-enter to resume polling'."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index("_credStatus(code, username)")
        block = src[i:i + 1400]
        assert "h.session.detail" in block
        assert "why ||" in block, "the constant must be the FALLBACK, not the winner"
        assert "Utils.escapeHtml(String(h.session.detail))" in block, "server text is escaped"


class TestAHandleIsAnIdentityNotAServer:
    """PLATAUDIT finding 3. Mastodon account handles held an instance URL, because
    _HANDLE_KEYS derived them from `mast_instance_url`. That is not cosmetic: the
    manifest and resolve_account_id match on (platform, handle), so every account on
    one instance shared a natural key.
    """

    def test_the_handle_prefers_the_real_identity(self):
        from database import accounts
        assert accounts._HANDLE_KEYS["mast"][0] == "mast_handle"

    def test_the_instance_url_survives_as_a_last_resort(self):
        """An install that has not re-checked yet keeps its label rather than blanking."""
        from database import accounts
        assert "mast_instance_url" in accounts._HANDLE_KEYS["mast"]
        assert accounts._default_handle(
            "mast", {"mast_instance_url": "https://example.social"}) == "https://example.social"

    def test_the_identity_wins_when_both_exist(self):
        from database import accounts
        assert accounts._default_handle("mast", {
            "mast_handle": "@someone@example.social",
            "mast_instance_url": "https://example.social"}) == "@someone@example.social"

    def test_the_connect_route_persists_what_it_already_resolved(self):
        """validate_session has always returned '@user@instance'; it was thrown away."""
        src = open("routes/mast_api.py", encoding="utf-8").read()
        i = src.index("def mast_connect")
        assert '"mast_handle": handle,' in src[i:i + 3000]

    def test_disconnecting_clears_it(self):
        src = open("routes/mast_api.py", encoding="utf-8").read()
        i = src.index("def mast_disconnect")
        assert "mast_handle" in src[i:i + 400]

    def test_an_existing_install_heals_without_reconnecting(self):
        """The session check already calls validate_session -- persisting there means
        nobody has to know to go and press Connect again."""
        import inspect
        from polling import session_check
        src = inspect.getsource(session_check._validate)
        i = src.index('elif code == "mast"')
        assert 'config.save_settings({"mast_handle"' in src[i:i + 900]


class TestASecondAccountCanHaveAHandleAtAll:
    """PLATAUDIT finding 2. A FurryNetwork account had no handle recorded, so there
    was nothing to cross-check and artist-credit could not link it. _default_handle
    reads FLAT settings, which hold the DEFAULT account's values -- a second account
    keeps its username under acct_<id>_<field>, so its handle could never be derived.
    """

    def test_a_non_default_account_reads_its_own_keys(self, monkeypatch):
        import config
        from database import accounts
        monkeypatch.setattr(config, "get_settings",
                            lambda: {"fn_username": "TheDefault",
                                     "acct_28_fn_username": "SecondFur"})
        got = accounts.derive_account_handle("fn", 28, False, config.get_settings())
        assert got == "SecondFur", "the second account must not inherit the default's name"

    def test_the_default_account_is_unchanged(self, monkeypatch):
        import config
        from database import accounts
        s = {"fn_username": "TheDefault"}
        assert accounts.derive_account_handle("fn", 1, True, s) == "TheDefault"

    def test_a_url_is_recognised_as_the_wrong_kind_of_value(self):
        from database import accounts
        assert accounts._looks_like_a_url("https://example.social")
        assert accounts._looks_like_a_url("HTTP://example.social")
        assert not accounts._looks_like_a_url("@someone@example.social")
        assert not accounts._looks_like_a_url("-1001234567890")   # a Telegram channel
        assert not accounts._looks_like_a_url("SecondFur")


class TestTheHandleBackfillRepairsWithoutInventing:
    """The migration that closes findings 2 and 3 on rows that already exist."""

    @pytest.fixture
    def conn(self, tmp_path):
        import sqlite3
        from database import accounts
        c = sqlite3.connect(tmp_path / "a.db")
        c.row_factory = sqlite3.Row
        accounts.ensure_accounts_table(c)
        return c

    def _add(self, c, platform, handle, is_default=1):
        cur = c.execute(
            "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order)"
            " VALUES (?, ?, ?, 1, ?, 0)", (platform, platform, handle, is_default))
        return cur.lastrowid

    def test_a_url_handle_is_replaced(self, conn):
        from database import accounts
        aid = self._add(conn, "mast", "https://example.social")
        n = accounts.backfill_account_handles(
            conn, {"mast_handle": "@someone@example.social"})
        assert n == 1
        assert conn.execute("SELECT handle FROM accounts WHERE account_id=?",
                            (aid,)).fetchone()["handle"] == "@someone@example.social"

    def test_an_empty_handle_is_filled(self, conn):
        from database import accounts
        aid = self._add(conn, "fn", "", is_default=0)
        accounts.backfill_account_handles(conn, {f"acct_{aid}_fn_username": "SecondFur"})
        assert conn.execute("SELECT handle FROM accounts WHERE account_id=?",
                            (aid,)).fetchone()["handle"] == "SecondFur"

    def test_a_good_handle_is_never_touched(self, conn):
        """The audit's own lesson: ownership is read, never inferred."""
        from database import accounts
        aid = self._add(conn, "fn", "TheOwner")
        assert accounts.backfill_account_handles(
            conn, {"fn_username": "SomethingElse"}) == 0
        assert conn.execute("SELECT handle FROM accounts WHERE account_id=?",
                            (aid,)).fetchone()["handle"] == "TheOwner"

    def test_nothing_knowable_means_nothing_written(self, conn):
        from database import accounts
        aid = self._add(conn, "fn", "")
        assert accounts.backfill_account_handles(conn, {}) == 0
        assert conn.execute("SELECT handle FROM accounts WHERE account_id=?",
                            (aid,)).fetchone()["handle"] == ""

    def test_it_is_idempotent(self, conn):
        from database import accounts
        self._add(conn, "mast", "https://example.social")
        s = {"mast_handle": "@someone@example.social"}
        assert accounts.backfill_account_handles(conn, s) == 1
        assert accounts.backfill_account_handles(conn, s) == 0

    def test_a_url_is_never_written_as_a_replacement(self, conn):
        """Falling back to the instance URL is right for a LABEL and wrong for a repair --
        it would 'fix' an empty handle into the same collision the row came from."""
        from database import accounts
        aid = self._add(conn, "mast", "")
        assert accounts.backfill_account_handles(
            conn, {"mast_instance_url": "https://example.social"}) == 0
        assert conn.execute("SELECT handle FROM accounts WHERE account_id=?",
                            (aid,)).fetchone()["handle"] == ""


class TestE621RecordsWhoActuallyUploaded:
    """PLATAUDIT finding 1. One post sat under one account that e621's API reports as
    another account's upload. It could not be confirmed locally because no local column
    held the answer -- `username` looked like one, but the client stamped its OWN
    configured name on every row, so the column asserted ownership it never read.

    The row stays unrepaired on purpose (ownership is knowable only from e621). What
    changes is that the disagreement is now VISIBLE instead of living in a backlog cell.
    """

    def test_the_client_records_the_payloads_uploader(self):
        src = open("clients/e621/client.py", encoding="utf-8").read()
        assert '"uploader_id": str(p.get("uploader_id") or "")' in src

    def test_the_stamped_username_is_labelled_as_an_assumption(self):
        """It stays for display continuity, but nothing should read it as attribution."""
        src = open("clients/e621/client.py", encoding="utf-8").read()
        i = src.index('"uploader_id": str(p.get("uploader_id")')
        assert "NOT a reading" in src[max(0, i - 400):i]

    def test_the_column_exists_and_is_stored(self):
        sql = open("database/e621_schema.sql", encoding="utf-8").read()
        assert "uploader_id" in sql
        q = open("database/e621_queries.py", encoding="utf-8").read()
        assert "uploader_id" in q

    def test_a_failed_detail_fetch_cannot_blank_a_recorded_uploader(self):
        """_empty_detail answers '' -- an unconditional upsert would erase attribution."""
        q = open("database/e621_queries.py", encoding="utf-8").read()
        assert "uploader_id=CASE WHEN excluded.uploader_id != ''" in q


class TestTheUploaderAuditReportsDisagreementNotOwnership:

    @pytest.fixture
    def conn(self, tmp_path):
        import sqlite3
        c = sqlite3.connect(tmp_path / "e.db")
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE e621_submissions (
            submission_id TEXT PRIMARY KEY, account_id INTEGER, uploader_id TEXT DEFAULT '',
            title TEXT DEFAULT '', link TEXT DEFAULT '', posted_at TEXT)""")
        return c

    def _add(self, c, sid, aid, uploader):
        c.execute("INSERT INTO e621_submissions (submission_id, account_id, uploader_id)"
                  " VALUES (?, ?, ?)", (str(sid), aid, uploader))

    def test_the_odd_one_out_is_flagged(self, conn):
        from database import e621_queries as q
        for i in range(5):
            self._add(conn, 100 + i, 21, "383000")
        self._add(conn, 6640148, 21, "383246")
        found = q.audit_uploader_disagreements(conn)
        assert len(found) == 1
        assert found[0]["submission_id"] == "6640148"
        assert found[0]["uploader_id"] == "383246"
        assert found[0]["account_usual_uploader_id"] == "383000"
        assert found[0]["siblings_agreeing"] == 5

    def test_a_consistent_account_reports_nothing(self, conn):
        from database import e621_queries as q
        for i in range(4):
            self._add(conn, 200 + i, 21, "383000")
        assert q.audit_uploader_disagreements(conn) == []

    def test_rows_never_repolled_are_silent_not_suspicious(self, conn):
        """A blank uploader means 'not read yet', which is not a disagreement."""
        from database import e621_queries as q
        for i in range(3):
            self._add(conn, 300 + i, 21, "383000")
        self._add(conn, 399, 21, "")
        assert q.audit_uploader_disagreements(conn) == []

    def test_accounts_are_judged_separately(self, conn):
        """Two accounts each internally consistent must not flag each other."""
        from database import e621_queries as q
        for i in range(3):
            self._add(conn, 400 + i, 20, "383246")
        for i in range(3):
            self._add(conn, 500 + i, 21, "383000")
        assert q.audit_uploader_disagreements(conn) == []

    def test_it_changes_nothing(self, conn):
        """It reports; repairing is the owner's call, because e621 is the only source
        that actually knows."""
        from database import e621_queries as q
        for i in range(3):
            self._add(conn, 600 + i, 21, "383000")
        self._add(conn, 699, 21, "383246")
        before = conn.execute("SELECT submission_id, account_id, uploader_id "
                              "FROM e621_submissions ORDER BY submission_id").fetchall()
        q.audit_uploader_disagreements(conn)
        after = conn.execute("SELECT submission_id, account_id, uploader_id "
                             "FROM e621_submissions ORDER BY submission_id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]

    def test_the_docstring_refuses_to_claim_ownership(self):
        from database import e621_queries as q
        doc = q.audit_uploader_disagreements.__doc__ or ""
        assert "does NOT say who owns" in doc
