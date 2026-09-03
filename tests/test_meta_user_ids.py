"""The Meta "User ID" box took a handle, and every call 400d (4.3.6).

A tester's log, twenty polls deep and otherwise healthy:

    IG: auth error (400) Object with ID '<handle>' does not exist, cannot be
    loaded due to missing permissions, or does not support this operation

Instagram's Graph API takes the user id in the URL **path** —
``/{user_id}/media`` — and PawPoller pastes whatever is in the Settings box
straight into it. The box says "User ID (optional)", so a handle typed there
was accepted, stored, and used forever. Two separate failures follow:

  1. Nothing Instagram could answer was ever requested, so polling found zero
     posts and posting could not work either.
  2. The client called the result an **auth error**, so the report read as a
     credentials problem — for a token Meta was happy with.

The same box, the same URL shape and the same log line exist in the Threads
client, which was written from the Instagram one. Both are fixed here, through
``clients/meta_graph.py``, so a third Meta client cannot inherit it.
"""
from __future__ import annotations

import pytest

import config
from clients.ig.client import IgClient
from clients.thr.client import ThrClient
from clients.meta_graph import graph_4xx_message, numeric_id


class _Resp:
    def __init__(self, status_code=400, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if (payload is not None or text) else b""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# The error Meta returned, with the handle replaced.
OBJECT_MISSING = {"error": {
    "message": ("Object with ID 'sample_handle' does not exist, cannot be loaded due to "
                "missing permissions, or does not support this operation"),
    "type": "IGApiException", "code": 803,
}}


class TestNumericId:
    @pytest.mark.parametrize("raw", ["17841400000000000", " 17841400000000000 ", "1"])
    def test_a_real_id_survives(self, raw):
        assert numeric_id(raw) == raw.strip()

    @pytest.mark.parametrize("raw", ["sample_handle", "@sample_handle", "user.name",
                                     "1784140000000000a", "", None, "  "])
    def test_anything_that_is_not_a_number_is_discarded(self, raw):
        assert numeric_id(raw) == "", (
            "a non-id must be blanked so validate_session() asks Meta, which knows")

    def test_an_at_prefixed_number_is_still_a_number(self):
        """People paste "@" out of habit; that alone should not lose the id."""
        assert numeric_id("@17841400000000000") == "17841400000000000"


@pytest.mark.parametrize("cls", [IgClient, ThrClient])
class TestNeitherClientPutsAHandleInAUrl:
    def test_a_handle_never_reaches_the_url(self, cls):
        c = cls(access_token="tok", user_id="sample_handle")
        assert c.user_id == "", "this value is interpolated into /{user_id}/media"

    def test_what_was_discarded_is_remembered(self, cls):
        """So the connect route can say what it did instead of silently doing
        something other than what was typed."""
        c = cls(access_token="tok", user_id="sample_handle")
        assert c.ignored_user_id == "sample_handle"

    def test_a_real_id_is_kept_and_nothing_is_flagged(self, cls):
        c = cls(access_token="tok", user_id="17841400000000000")
        assert c.user_id == "17841400000000000"
        assert c.ignored_user_id == ""

    def test_update_credentials_applies_the_same_rule(self, cls):
        c = cls(access_token="tok", user_id="17841400000000000")
        c.update_credentials("tok2", "sample_handle")
        assert c.user_id == ""
        assert c.ignored_user_id == "sample_handle"

    def test_an_empty_id_is_not_reported_as_ignored(self, cls):
        assert cls(access_token="tok").ignored_user_id == ""


class TestTheErrorIsNotCalledAnAuthFailure:
    def test_code_803_names_the_real_problem(self):
        msg = graph_4xx_message("Instagram", _Resp(400, OBJECT_MISSING))
        assert "NUMERIC" in msg and "handle" in msg
        for wrong in ("expired", "invalid"):
            assert wrong not in msg.lower(), (
                f"{wrong!r}: the token was fine — Meta did not recognise the ID")

    def test_the_prose_alone_is_enough_when_the_code_is_missing(self):
        payload = {"error": {"message": "Object with ID 'x' does not exist"}}
        assert "NUMERIC" in graph_4xx_message("Threads", _Resp(400, payload))

    def test_a_genuinely_dead_token_still_says_so(self):
        payload = {"error": {"message": "Session has expired", "code": 190}}
        msg = graph_4xx_message("Instagram", _Resp(401, payload))
        assert "expired or invalid" in msg.lower()

    def test_anything_else_quotes_meta_and_the_status(self):
        payload = {"error": {"message": "Application request limit reached", "code": 4}}
        msg = graph_4xx_message("Instagram", _Resp(400, payload))
        assert "Application request limit reached" in msg and "400" in msg

    @pytest.mark.parametrize("resp", [
        _Resp(400, None, "<html>gateway</html>"),
        _Resp(400, {"error": "a string, not an object"}),
        _Resp(400, {}),
        _Resp(400),
    ])
    def test_a_malformed_body_does_not_crash(self, resp):
        assert graph_4xx_message("Instagram", resp)

    def test_both_clients_route_their_4xx_through_it(self):
        for path, name in (("clients/ig/client.py", "Instagram"),
                           ("clients/thr/client.py", "Threads")):
            src = open(path, encoding="utf-8").read()
            assert f'graph_4xx_message("{name}", resp)' in src
            assert "auth error (%s)" not in src, (
                f"{path} still calls every 4xx an auth error")


class TestMigration:
    @pytest.fixture()
    def settings(self, monkeypatch):
        store = {}
        monkeypatch.setattr(config, "get_settings", lambda: dict(store))
        monkeypatch.setattr(config, "save_settings", lambda d: store.update(d))
        return store

    def test_a_stored_handle_is_cleared_so_the_id_can_be_resolved(self, settings):
        settings.update({"ig_user_id": "sample_handle", "ig_access_token": "tok"})
        assert config.migrate_meta_user_ids() == 1
        assert settings["ig_user_id"] == ""

    def test_the_handle_is_kept_where_it_belongs(self, settings):
        """It is real information the user typed — move it, don't bin it."""
        settings.update({"ig_user_id": "sample_handle"})
        config.migrate_meta_user_ids()
        assert settings["ig_username"] == "sample_handle"

    def test_threads_too(self, settings):
        settings.update({"thr_user_id": "@sample_handle"})
        config.migrate_meta_user_ids()
        assert settings["thr_user_id"] == ""
        assert settings["thr_username"] == "sample_handle"

    def test_a_real_id_is_left_alone(self, settings):
        settings.update({"ig_user_id": "17841400000000000"})
        assert config.migrate_meta_user_ids() == 0
        assert settings["ig_user_id"] == "17841400000000000"

    def test_per_account_copies_are_fixed_too(self, settings):
        settings.update({"acct_3_ig_user_id": "sample_handle"})
        config.migrate_meta_user_ids()
        assert settings["acct_3_ig_user_id"] == ""
        assert settings["acct_3_ig_username"] == "sample_handle"

    def test_an_existing_username_is_never_overwritten(self, settings):
        settings.update({"ig_user_id": "sample_handle", "ig_username": "SecondHandle"})
        config.migrate_meta_user_ids()
        assert settings["ig_username"] == "SecondHandle"
        assert settings["ig_user_id"] == ""

    def test_it_is_safe_to_run_twice(self, settings):
        settings.update({"ig_user_id": "sample_handle"})
        assert config.migrate_meta_user_ids() == 1
        assert config.migrate_meta_user_ids() == 0

    def test_nothing_to_do_is_a_no_op(self, settings):
        assert config.migrate_meta_user_ids() == 0

    def test_it_runs_on_both_entry_points(self):
        """The desktop boots through dashboard.py's lifespan and the server
        through server.py's main(); a migration in only one reaches only half
        the installs."""
        for path in ("dashboard.py", "server.py"):
            src = open(path, encoding="utf-8").read()
            assert "config.migrate_meta_user_ids()" in src, f"{path} never runs it"


class TestTheConnectRouteSaysWhatItDid:
    @pytest.mark.parametrize("path", ["routes/ig_api.py", "routes/thr_api.py"])
    def test_a_replaced_id_is_reported_not_swallowed(self, path):
        src = open(path, encoding="utf-8").read()
        assert "client.ignored_user_id" in src
        assert "read from your access token" in src


class TestTheSettingsFormNoLongerInvitesIt:
    @pytest.mark.parametrize("field", ["ig-user-id", "thr-user-id"])
    def test_the_box_asks_for_a_number(self, field):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index(f'id="{field}"')
        placeholder = src[i:i + 200]
        assert "Numeric" in placeholder, (
            'the old placeholder was "User ID (optional)", which is what invited '
            "a handle in the first place")
