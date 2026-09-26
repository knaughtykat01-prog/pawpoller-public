"""Route-level tests for the Trello mirror API (spec 006).

`test_trello_mirror.py` and `test_trello_outbox.py` prove the engine. These prove
the *wiring*, which is where the damage would be:

* a route that talks to Trello directly would block the page on the network and
  skip the outbox's ordering and retry;
* a `/test` route that echoes the upstream body hands back the credential;
* an ungated route on an instance with no password is a remote caller driving the
  operator's boards;
* a delete route without its confirmation is an unrecoverable action one click
  away.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import config
from clients.trello.client import TrelloAuthError
from database.db import get_connection
from routes import trello_api
from trello import mapping, mirror

KEY = "abcdef0123456789"
TOKEN = "fedcba9876543210"


class FakeClient:
    fail = None

    def __init__(self, key, token):
        pass

    def me(self):
        if FakeClient.fail:
            raise FakeClient.fail
        return {"id": "M1", "username": "someone", "full_name": "Some One"}


def _seed():
    conn = get_connection()
    try:
        read = {"id": "B1", "name": "Sample board", "prefs": {},
                "lists": [{"id": "L1", "name": "To do", "pos": 1},
                          {"id": "L2", "name": "Done", "pos": 2}],
                "cards": [{"id": f"C{i}", "name": f"Card {i}", "pos": i, "idList": "L1",
                           "idLabels": [], "badges": {}} for i in (1, 2, 3)],
                "labels": [{"id": "LB1", "name": "Urgent", "color": "red"}],
                "checklists": [{"id": "K1", "idCard": "C1", "name": "Steps", "pos": 1,
                                "checkItems": [{"id": "I1", "name": "a", "pos": 1,
                                                "state": "incomplete"}]}]}
        mirror.apply_board(conn, read, [
            {"id": "A1", "date": "2026-01-01", "idMemberCreator": "M1",
             "memberCreator": {"fullName": "Some One"}, "data": {"text": "mine", "card": {"id": "C1"}}},
            {"id": "A2", "date": "2026-01-02", "idMemberCreator": "M2",
             "memberCreator": {"fullName": "Other"}, "data": {"text": "theirs", "card": {"id": "C1"}}}])
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(monkeypatch):
    # /api/trello is in _SENSITIVE_WHEN_OPEN_PREFIXES, so an unconfigured instance
    # refuses it to a non-loopback caller — and TestClient is not loopback.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda token: token == "pp_test")
    FakeClient.fail = None
    monkeypatch.setattr(trello_api, "TrelloClient", FakeClient)
    config.save_settings({"trello_api_key": KEY, "trello_token": TOKEN})
    mapping.save_config(member={"id": "M1", "username": "someone", "full_name": "Some One"})
    _seed()
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False,
                      headers={"Authorization": "Bearer pp_test"})


def _ops():
    conn = get_connection()
    try:
        return [(r["op"], r["object_id"], json.loads(r["fields"]))
                for r in conn.execute("SELECT * FROM trello_outbox ORDER BY seq")]
    finally:
        conn.close()


class TestTheRoutesAreGated:

    def test_every_trello_route_needs_auth(self, monkeypatch):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
        monkeypatch.setattr(config, "validate_api_key", lambda token: False)
        import dashboard
        anon = TestClient(dashboard.app, raise_server_exceptions=False)
        for method, path in (("get", "/api/trello/config"), ("get", "/api/trello/status"),
                             ("get", "/api/trello/boards"), ("post", "/api/trello/import"),
                             ("patch", "/api/trello/cards/C1"),
                             ("get", "/api/trello/covers/abc")):
            r = getattr(anon, method)(path, **({} if method == "get" else {"json": {}}))
            assert r.status_code in (401, 403), f"{path} answered {r.status_code}"

    def test_the_prefix_is_on_the_sensitive_list(self):
        import dashboard
        assert "/api/trello" in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES


class TestCredentialsNeverTravelBack:

    def test_a_good_test_returns_the_account_not_the_credential(self, client):
        body = client.post("/api/trello/test", json={"key": KEY, "token": TOKEN}).json()
        assert body["ok"] is True and body["member"]["username"] == "someone"
        assert KEY not in str(body) and TOKEN not in str(body)

    def test_a_failure_says_so_without_echoing_anything(self, client):
        FakeClient.fail = TrelloAuthError("Trello did not accept that API key and token.")
        body = client.post("/api/trello/test", json={"key": KEY, "token": TOKEN}).json()
        assert body["ok"] is False
        assert KEY not in body["error"] and TOKEN not in body["error"]

    def test_the_config_route_reports_presence_not_the_values(self, client):
        config.save_settings({"trello_secret": "s" * 32})
        body = client.get("/api/trello/config").json()
        assert body["has_credentials"] is True and body["has_secret"] is True
        assert KEY not in str(body) and TOKEN not in str(body) and "s" * 32 not in str(body)


class TestSavingTheCredentials:
    """⚠ 4.36.4: the panel once called a save helper that never existed, so nothing
    was ever stored. The route and the round trip are pinned here."""

    def test_the_route_stores_all_three(self, client):
        client.post("/api/trello/credentials",
                    json={"key": "k" * 16, "token": "t" * 16, "secret": "s" * 16})
        s = config.get_settings()
        assert (s.get("trello_api_key"), s.get("trello_token"), s.get("trello_secret")) == \
            ("k" * 16, "t" * 16, "s" * 16)

    def test_saving_only_one_does_not_blank_the_others(self, client):
        config.save_settings({"trello_api_key": "old-key", "trello_token": "keep-me"})
        client.post("/api/trello/credentials", json={"secret": "new-secret"})
        s = config.get_settings()
        assert s.get("trello_api_key") == "old-key" and s.get("trello_token") == "keep-me"

    def test_an_empty_body_is_refused(self, client):
        assert client.post("/api/trello/credentials", json={}).status_code == 400

    def test_it_reports_presence_without_echoing_the_values(self, client):
        body = client.post("/api/trello/credentials", json={"key": KEY, "token": TOKEN}).json()
        assert body["saved"] is True
        assert KEY not in str(body) and TOKEN not in str(body)

    def test_connecting_claims_the_connection(self, client):
        mapping.save_config(owner_tag="")
        client.post("/api/trello/credentials", json={"key": KEY, "token": TOKEN})
        assert mapping.get_config()["owner_tag"] == mapping.instance_tag()

    def test_all_three_land_in_the_vault_not_plaintext(self):
        config.save_settings({"trello_api_key": "a1", "trello_token": "b2", "trello_secret": "c3"})
        plain = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        assert not {"trello_api_key", "trello_token", "trello_secret"} & set(plain)


class TestReadsComeFromTheMirror:

    def test_the_board_list(self, client):
        boards = client.get("/api/trello/boards").json()["boards"]
        assert [b["id"] for b in boards] == ["B1"] and boards[0]["imported"] is True

    def test_a_board_carries_everything_the_face_needs_in_order(self, client):
        b = client.get("/api/trello/boards/B1").json()
        assert [l["id"] for l in b["lists"]] == ["L1", "L2"]
        assert [c["id"] for c in b["cards"]] == ["C1", "C2", "C3"]
        c1 = b["cards"][0]
        assert c1["checklist"] == {"done": 0, "total": 1}
        assert set(c1) >= {"labels", "cover", "cover_url", "has_desc", "comment_count",
                           "is_commission", "pending", "conflict"}

    def test_a_card_view_marks_only_my_comments_editable(self, client):
        v = client.get("/api/trello/cards/C1").json()
        mine = {c["text"]: c["is_mine"] for c in v["comments"]}
        assert mine == {"mine": True, "theirs": False}
        assert v["checklists"][0]["items"][0]["id"] == "I1"

    def test_hiding_a_board_is_local_only(self, client):
        client.patch("/api/trello/boards/B1", json={"hidden": True})
        assert client.get("/api/trello/boards").json()["boards"] == []
        assert _ops() == []


class TestWritesQueueRatherThanCallTrello:

    def test_a_new_card_gets_a_temp_id_and_one_create_op(self, client):
        r = client.post("/api/trello/lists/L2/cards", json={"name": "New"}).json()
        assert r["id"].startswith("tmp_") and r["pending"] is True
        assert _ops() == [("card.create", r["id"], {"list_id": "L2", "name": "New",
                                                     "pos": r["pos"]})]

    def test_a_drop_between_two_cards_lands_between_them(self, client):
        r = client.post("/api/trello/cards/C3/move",
                        json={"list_id": "L1", "before_id": "C1", "after_id": "C2"}).json()
        assert 1 < r["pos"] < 2
        assert [c["id"] for c in client.get("/api/trello/boards/B1").json()["cards"]] == \
            ["C1", "C3", "C2"]

    def test_a_patch_queues_only_the_fields_given(self, client):
        client.patch("/api/trello/cards/C1", json={"desc": "x", "due_complete": True})
        assert _ops() == [("card.update", "C1", {"desc": "x", "due_complete": True})]

    def test_a_label_toggle_queues_the_target_set(self, client):
        client.post("/api/trello/cards/C1/labels", json={"label_id": "LB1"})
        assert _ops()[-1] == ("card.labels", "C1", {"labels": ["LB1"]})

    def test_a_cover_upload_must_really_be_an_image(self, client):
        """The declared type is the browser's claim; the bytes must agree."""
        r = client.post("/api/trello/cards/C1/cover",
                        files={"file": ("x.png", b"<svg onload=alert(1)>", "image/png")})
        assert r.status_code == 400
        ok = client.post("/api/trello/cards/C1/cover",
                         files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")})
        assert ok.status_code == 200 and _ops()[-1][0] == "cover.upload"

    def test_an_unknown_cover_colour_is_refused(self, client):
        assert client.put("/api/trello/cards/C1/cover", json={"color": "chartreuse"}).status_code == 400

    @pytest.mark.parametrize("path", ["/api/trello/items/I1", "/api/trello/checklists/K1",
                                      "/api/trello/labels/LB1", "/api/trello/comments/A1"])
    def test_every_delete_needs_its_confirmation(self, client, path):
        """FR-027: Trello has no undo for these."""
        assert client.delete(path).status_code == 400
        assert client.delete(path + "?confirm=1").status_code == 200

    def test_someone_elses_comment_cannot_be_changed(self, client):
        assert client.patch("/api/trello/comments/A2", json={"text": "no"}).status_code == 403
        assert client.delete("/api/trello/comments/A2?confirm=1").status_code == 403

    def test_there_is_no_route_that_deletes_a_card_list_or_board(self):
        for route in trello_api.trello_router.routes:
            if "DELETE" in getattr(route, "methods", set()):
                assert not re.fullmatch(r"/api/trello/(cards|lists|boards)/\{[a-z_]+\}", route.path), \
                    route.path


class TestConflicts:

    def _conflict(self, client):
        client.patch("/api/trello/cards/C1", json={"desc": "mine"})
        conn = get_connection()
        try:
            read = json.loads(json.dumps({"id": "B1", "name": "Sample board", "prefs": {},
                    "lists": [{"id": "L1", "pos": 1}, {"id": "L2", "pos": 2}],
                    "cards": [{"id": f"C{i}", "name": f"Card {i}", "pos": i, "idList": "L1",
                               "desc": "theirs" if i == 1 else "", "badges": {}} for i in (1, 2, 3)],
                    "labels": [{"id": "LB1", "name": "Urgent", "color": "red"}],
                    "checklists": [{"id": "K1", "idCard": "C1", "name": "Steps", "pos": 1,
                                    "checkItems": [{"id": "I1", "name": "a", "pos": 1}]}]}))
            mirror.apply_board(conn, read)
            conn.commit()
        finally:
            conn.close()

    def test_a_conflict_shows_both_values_on_the_card(self, client):
        self._conflict(client)
        c = client.get("/api/trello/cards/C1").json()["conflicts"]
        assert [(x["field"], x["local"], x["remote"]) for x in c] == [("desc", "mine", "theirs")]

    def test_keeping_trellos_drops_our_op(self, client):
        self._conflict(client)
        client.post("/api/trello/conflicts/resolve", json={
            "object_type": "card", "object_id": "C1", "field": "desc", "keep": "trello"})
        assert _ops() == []
        assert client.get("/api/trello/cards/C1").json()["card"]["desc"] == "theirs"

    def test_keeping_ours_releases_the_held_op(self, client):
        self._conflict(client)
        client.post("/api/trello/conflicts/resolve", json={
            "object_type": "card", "object_id": "C1", "field": "desc", "keep": "pawpoller"})
        conn = get_connection()
        try:
            assert conn.execute("SELECT state FROM trello_outbox").fetchone()["state"] == "pending"
        finally:
            conn.close()

    def test_resolving_needs_a_real_side(self, client):
        r = client.post("/api/trello/conflicts/resolve", json={
            "object_type": "card", "object_id": "C1", "field": "desc", "keep": "both"})
        assert r.status_code == 400


class TestNothingPersonalLeavesTheApp:

    def test_no_log_call_is_handed_a_credential_or_board_content(self):
        """What a log call is GIVEN, not what its message says: every argument after
        the format string must be an exception or its type name. Card text,
        comments, member names and client names are personal data (FR-034)."""
        allowed = {"e", "type(e).__name__"}
        for path in ("clients/trello/client.py", "routes/trello_api.py",
                     "routes/trello_hooks.py", "polling/trello_mirror.py",
                     "trello/mirror.py", "trello/outbox.py", "trello/covers.py",
                     "trello/webhooks.py", "trello/commission.py"):
            src = open(path, encoding="utf-8").read()
            for m in re.finditer(r"logger\.\w+\(\s*(\"[^\"]*\"|'[^']*')\s*(,(.*))?\)\s*$", src,
                                 re.MULTILINE):
                args = {a.strip() for a in (m.group(3) or "").split(",") if a.strip()}
                assert args <= allowed, f"{path}: {m.group(0)}"


class TestBothEntryPointsStartTheMirror:
    """⚠ Found on the 4.36.0 deploy, not by a test: a thread wired into `main.py`
    but not `server.py`'s own table runs on the desktop and silently never on the
    server. Both mirror threads must be in both."""

    def test_the_desktop_entry_point_starts_both(self):
        src = open("main.py", encoding="utf-8").read()
        assert "run_trello_mirror" in src and "run_trello_outbox" in src

    def test_both_are_in_the_servers_thread_table(self):
        src = open("server.py", encoding="utf-8").read()
        i = src.index("threads = [")
        table = src[i:src.index("]", i)]
        assert "run_trello_mirror" in table and "run_trello_outbox" in table

    def test_every_scheduler_the_desktop_runs_the_server_runs_too(self):
        main_src = open("main.py", encoding="utf-8").read()
        server_src = open("server.py", encoding="utf-8").read()
        i = server_src.index("threads = [")
        table = server_src[i:server_src.index("]", i)]
        for scheduler in ("start_posting_scheduler", "run_auto_backup_scheduler",
                          "run_trello_mirror", "run_trello_outbox"):
            assert scheduler in main_src, f"{scheduler} is missing from main.py"
            assert scheduler in table, f"{scheduler} is missing from server.py's thread table"

    def test_the_005_scheduler_is_gone_from_both(self):
        for path in ("main.py", "server.py"):
            assert "run_trello_scheduler" not in open(path, encoding="utf-8").read()


def _guide_text() -> str:
    """The Trello guide as a READER sees it.

    ⚠ The source joins prose with `' + '` across line breaks, so a phrase that is
    plainly in the guide is often not a substring of the file. Assertions that
    searched the raw source failed three times on text that was right there.
    """
    src = open("frontend/js/platform_guides.js", encoding="utf-8").read()
    i = src.index("    trello: {")
    block = src[i:src.index("\n    },", i)]
    return re.sub(r"'\s*\+\s*'", "", block)


class TestTheSetupGuideAndTheConnectButton:
    """Getting a Trello API key is the hard part of this feature, and none of it is
    in PawPoller: you have to create a "Power-Up" on a developer page nobody finds
    by accident, and the form asks for an **Iframe connector URL** that a REST-only
    integration does not have. Someone who guesses at that box is stuck before they
    reach anything this codebase controls -- so the guide is part of the feature,
    not decoration around it.
    """

    def _guide(self):
        return _guide_text()

    def test_there_is_a_trello_guide(self):
        from pathlib import Path
        src = open("frontend/js/platform_guides.js", encoding="utf-8").read()
        assert "    trello: {" in src
        assert Path("frontend/img/guides/trello/guide.pdf").exists(), \
            "re-run deploy/make_guide_pdfs.py"

    def test_it_answers_the_iframe_connector_url_question(self):
        """⚠ The single question that blocks setup, and the answer is NOT "leave it
        blank" -- Trello refuses to save a Power-Up without one. An earlier draft of
        this guide said blank; this assertion exists so it cannot drift back.

        The deeper assertion lives in TestGettingTheTokenNeedsNoCallbackUrl; this
        one just pins that the step is present and names an address."""
        g = self._guide()
        assert "Iframe connector URL" in g
        # The real answer, read off the live form: a radio ABOVE the name box.
        # Choosing "doesn't use Power-up capabilities" removes the field entirely.
        assert "use Power-up capabilities" in g
        assert "cannot be changed after" in g, \
            "the choice is permanent -- the guide has to say so"

    def test_it_says_where_the_key_and_token_come_from(self):
        g = self._guide()
        assert "power-ups/admin" in g
        # Verified against the live admin: Authorization -> Trello Auth -> Generate.
        # There is no "API Key" tab; older docs say there is.
        assert "Authorization" in g and "Trello Auth" in g
        assert "Generate a new Trello Auth API key" in g

    def test_it_warns_about_a_token_that_expires(self):
        """A token with an expiry stops the sync on its day with no warning."""
        g = self._guide()
        assert "never expires" in g.lower() or "never expire" in g.lower()

    def test_it_explains_what_the_secret_is_for(self):
        """⚠ Read off the live API key page: it shows THREE long strings -- API key,
        Allowed origins, and a **Secret**. Until 4.37.0 the Secret was unused and the
        guide warned readers off it; live updates (spec 006) sign every webhook
        with it, so the guide now says what it is for -- and still says it cannot
        be reset, which is the reason to keep it in one place."""
        g = _guide_text()
        assert "Secret" in g
        assert "live updates" in g.lower()
        assert "no way to reset" in g
    def test_it_says_allowed_origins_stays_empty(self):
        """The box sits directly under the API key and looks like something to fill
        in. It only constrains redirects, and nothing here redirects."""
        g = _guide_text()
        assert "Allowed origins" in g
        assert "leave empty" in g.lower() or "nothing to put in" in g.lower()

    def test_the_guide_has_the_fields_the_renderer_needs(self):
        g = self._guide()
        for field in ("steps:", "paste:", "renew:", "need:", "notes:"):
            assert field in g, field

    def test_a_guide_with_no_platform_row_still_gets_a_readable_title(self):
        """Trello is not in PLATFORMS -- it is a commissions board, not a place you
        post to -- so label() would render it "TRELLO" without the fallback."""
        src = open("frontend/js/platform_guides.js", encoding="utf-8").read()
        i = src.index("function label(code)")
        assert "GUIDES[code].title" in src[i:i + 400]
        assert "title: 'Trello'" in self._guide()

    def test_settings_offers_the_guide(self):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        assert "trello-guide-btn" in src
        assert 'openModal("trello")' in src

    def test_the_commissions_board_has_a_connect_button(self):
        src = open("frontend/js/commissions.js", encoding="utf-8").read()
        assert "data-trello-connect" in src
        assert "_trelloStrip" in src

    def test_connect_opens_the_guide_rather_than_dumping_you_in_settings(self):
        """Settings has two empty boxes and no way to fill them; the guide is the
        part that is actually missing."""
        src = open("frontend/js/commissions.js", encoding="utf-8").read()
        i = src.index("data-trello-connect]")
        assert "openModal('trello')" in src[i:i + 500]


class TestGettingTheTokenNeedsNoCallbackUrl:
    """⚠ The two questions that actually block setup, and the answers they need.

    Trello will not save a Power-Up without an **Iframe connector URL**, and the
    authorize flow looks like it wants a **callback URL**. Neither has an obvious
    answer for an app that runs on your own machine, and guessing at either leaves
    you stuck before you reach anything this codebase controls.

    The token half is answered in code rather than in prose: PawPoller builds the
    authorize URL itself, using `response_type=token` with no `return_url` and no
    `callback_method`, so Trello displays the token instead of redirecting and
    there is nothing to add to the Power-Up's allowed origins.
    """

    def _handler(self):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index('trello-token-btn")?.addEventListener')
        return src[i:i + 1400]

    def test_the_panel_builds_the_authorize_url(self):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        assert "trello-token-btn" in src
        assert "trello.com/1/authorize" in src

    def test_it_asks_trello_to_display_the_token_not_redirect(self):
        """This single parameter is why no callback URL is needed."""
        h = self._handler()
        assert "response_type=token" in h

    def test_it_sends_no_return_url_and_no_callback_method(self):
        """⚠ Either one turns this into a redirect flow, which then REQUIRES the
        origin to be on the Power-Up's allow-list -- the exact dead end the button
        exists to avoid."""
        h = self._handler()
        assert "return_url" not in h
        assert "callback_method" not in h

    def test_the_token_is_asked_to_never_expire(self):
        """An expiring token stops the sync on its day, silently."""
        assert "expiration=never" in self._handler()

    def test_it_asks_for_write_as_well_as_read(self):
        """Read-only would sync one way and fail every push with a 401."""
        assert "scope=read,write" in self._handler()

    def test_the_key_is_url_encoded(self):
        assert "encodeURIComponent" in self._handler()

    def test_it_refuses_politely_with_no_key(self):
        """The link is built FROM the key, so there is nothing to open without one."""
        h = self._handler()
        assert "if (!key)" in h

    def test_the_guide_answers_the_iframe_box_rather_than_dodging_it(self):
        """⚠ An earlier draft of this guide said to leave the box blank. Trello does
        not allow that -- it will not save the Power-Up -- so the guide has to name
        something to put in it."""
        g = _guide_text()
        assert "Iframe connector URL" in g
        assert "not let you save" in g, \
            "the guide must say the field is mandatory, not optional"
        # And the escape hatch for an app already created the other way.
        assert "pawpoller.pages.dev" in g

    def test_the_guide_says_no_callback_url_is_needed(self):
        assert "callback URL" in _guide_text()


