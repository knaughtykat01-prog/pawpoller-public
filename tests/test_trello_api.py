"""Route-level tests for the Trello API (spec 005).

`test_trello_sync.py` proves the engine and the orchestration. These prove the
*wiring*, which is where the damage would be:

* a sync route that applies without confirmation writes to a board other people
  can see, immediately and visibly;
* a `/test` route that echoes the upstream body hands back the credential it was
  given, because Trello repeats query parameters in some error bodies;
* an ungated route on an instance with no dashboard password is a remote caller
  driving the operator's board.

None of those shows up in a test of `trello.sync`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from clients.trello.client import TrelloAuthError
from routes import trello_api
from trello import mapping, sync

LISTS = {"quote": "L1", "accepted": "L2", "wip": "L3", "paid": "L4", "delivered": "L5"}

KEY = "abcdef0123456789"
TOKEN = "fedcba9876543210"


class FakeTrello:
    def __init__(self, fail=None):
        self.fail = fail

    def me(self):
        if self.fail:
            raise self.fail
        return {"id": "m1", "username": "someone", "full_name": "Some One"}

    def boards(self):
        return [{"id": "B1", "name": "Board", "closed": False}]

    def lists(self, board_id):
        return [{"id": v, "name": k, "pos": i} for i, (k, v) in enumerate(LISTS.items())]

    def cards(self, board_id):
        return []

    def card(self, card_id):
        return None


@pytest.fixture
def client(monkeypatch):
    # /api/trello is in _SENSITIVE_WHEN_OPEN_PREFIXES, so an unconfigured instance
    # refuses it to a non-loopback caller — and TestClient is not loopback.
    # Authenticate properly rather than weakening the middleware.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda token: token == "pp_test")
    fake = FakeTrello()
    monkeypatch.setattr(trello_api, "_client_or_400", lambda body=None: fake)
    monkeypatch.setattr(sync, "client_from_settings", lambda settings=None: fake)
    config.save_settings({"trello_api_key": KEY, "trello_token": TOKEN})
    mapping.save_config(board_id="B1", list_map=dict(LISTS), first_sync_done=True)

    import dashboard
    c = TestClient(dashboard.app, raise_server_exceptions=False,
                   headers={"Authorization": "Bearer pp_test"})
    c.fake = fake
    return c


class TestTheRoutesAreGated:

    def test_every_trello_route_needs_auth(self, monkeypatch):
        monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
        monkeypatch.setattr(config, "validate_api_key", lambda token: False)
        import dashboard
        anon = TestClient(dashboard.app, raise_server_exceptions=False)
        for method, path in (("get", "/api/trello/config"),
                             ("get", "/api/trello/status"),
                             ("post", "/api/trello/preview"),
                             ("post", "/api/trello/sync")):
            r = (anon.get(path) if method == "get" else anon.post(path, json={}))
            assert r.status_code in (401, 403), f"{path} answered {r.status_code}"

    def test_the_prefix_is_on_the_sensitive_list(self):
        """An instance with no dashboard password still refuses these to a remote
        caller — the board is outward-facing and /test takes a credential."""
        import dashboard
        assert "/api/trello" in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES


class TestCredentialsNeverTravelBack:

    def test_a_good_test_returns_the_account_not_the_credential(self, client):
        body = client.post("/api/trello/test",
                           json={"key": KEY, "token": TOKEN}).json()
        assert body["ok"] is True
        assert body["member"]["username"] == "someone"
        assert KEY not in str(body) and TOKEN not in str(body)

    def test_a_failure_says_so_without_echoing_anything(self, client):
        client.fake.fail = TrelloAuthError(
            "Trello did not accept that API key and token. Check them in "
            "Settings -> Trello.")
        body = client.post("/api/trello/test",
                           json={"key": KEY, "token": TOKEN}).json()
        assert body["ok"] is False
        assert KEY not in body["error"] and TOKEN not in body["error"]

    def test_the_config_route_reports_presence_not_the_values(self, client):
        body = client.get("/api/trello/config").json()
        assert body["has_credentials"] is True
        assert KEY not in str(body) and TOKEN not in str(body)


class TestSyncWritesOnlyWhenTold:

    def test_preview_is_never_applied(self, client):
        assert client.post("/api/trello/preview").json()["applied"] is False

    def test_sync_without_confirm_previews(self, client):
        """⚠ The dangerous default. A sync that applies on a bare POST writes to a
        board other people can see."""
        assert client.post("/api/trello/sync", json={}).json()["applied"] is False

    def test_sync_with_confirm_applies(self, client):
        assert client.post("/api/trello/sync", json={"confirm": True}).json()["applied"] is True

    def test_the_first_sync_previews_even_with_confirm(self, client):
        """The rule lives in trello/sync.py, not in the route — a board-level rule
        does not belong in a request flag."""
        mapping.save_config(first_sync_done=False)
        body = client.post("/api/trello/sync", json={"confirm": True}).json()
        assert body["applied"] is False
        assert body["errors"]

    def test_a_second_sync_is_refused_rather_than_run_twice(self, client):
        trello_api._sync_lock.acquire()
        try:
            assert client.post("/api/trello/preview").status_code == 409
        finally:
            trello_api._sync_lock.release()


class TestConfiguration:

    def test_boards_and_lists_come_from_trello(self, client):
        assert client.get("/api/trello/boards").json()["boards"][0]["id"] == "B1"
        assert len(client.get("/api/trello/boards/B1/lists").json()["lists"]) == 5

    def test_picking_a_board_claims_it(self, client):
        body = client.put("/api/trello/config",
                          json={"board_id": "B2", "board_name": "Other"}).json()
        assert body["claimed"] is True
        assert body["owner_tag"] == mapping.instance_tag()
        assert body["first_sync_done"] is False

    def test_the_mapping_can_be_saved(self, client):
        body = client.put("/api/trello/config",
                          json={"list_map": {"wip": "L9"}}).json()
        assert body["list_map"]["wip"] == "L9"

    def test_an_empty_body_is_refused_rather_than_saving_nothing(self, client):
        assert client.put("/api/trello/config", json={}).status_code == 400

    def test_status_reports_without_touching_trello(self, client):
        body = client.get("/api/trello/status").json()
        assert body["configured"] is True and body["is_owner"] is True
        assert body["open_conflicts"] == 0


class TestConflictResolution:

    def _conflict(self, client):
        from database import trello_queries as tq
        from database.db import get_connection
        conn = get_connection()
        try:
            tq.create_link(conn, client_name="Sample Client", created_at="2026-01-01",
                           card_id="C1", board_id="B1")
            tq.open_conflict(conn, client_name="Sample Client",
                             created_at="2026-01-01", field="price",
                             baseline_value="100.0", local_value="150.0",
                             remote_value="200.0")
        finally:
            conn.close()

    def test_a_conflict_is_listed_with_both_values(self, client):
        self._conflict(client)
        row = client.get("/api/trello/conflicts").json()["conflicts"][0]
        assert row["local_value"] == "150.0" and row["remote_value"] == "200.0"

    def test_resolving_needs_a_real_side(self, client):
        self._conflict(client)
        r = client.post("/api/trello/conflicts/resolve",
                        json={"client_name": "Sample Client", "created_at": "2026-01-01",
                              "field": "price", "side": "whichever"})
        assert r.status_code == 400

    def test_resolving_needs_a_known_field(self, client):
        self._conflict(client)
        r = client.post("/api/trello/conflicts/resolve",
                        json={"client_name": "Sample Client", "created_at": "2026-01-01",
                              "field": "notes", "side": "local"})
        assert r.status_code == 400, "notes is not synced and cannot conflict"

    def test_an_unknown_conflict_is_a_404_not_a_silent_ok(self, client):
        r = client.post("/api/trello/conflicts/resolve",
                        json={"client_name": "Nobody", "created_at": "2026-01-01",
                              "field": "price", "side": "local"})
        assert r.status_code == 404

    def test_keeping_ours_clears_the_conflict_without_writing_to_trello(self, client):
        self._conflict(client)
        r = client.post("/api/trello/conflicts/resolve",
                        json={"client_name": "Sample Client", "created_at": "2026-01-01",
                              "field": "price", "side": "local"})
        assert r.json()["resolved"] is True
        assert client.get("/api/trello/conflicts").json()["conflicts"] == []

    def test_there_is_no_resolve_all_route(self, client):
        """A bulk button on a screen whose whole purpose is 'look at these two
        values' defeats the screen."""
        import dashboard
        paths = {r.path for r in dashboard.app.routes if hasattr(r, "path")}
        assert not any("resolve-all" in p or "resolve_all" in p for p in paths)


class TestUnlink:

    def test_unlink_keeps_the_commission(self, client):
        from database import commissions_queries as cq
        from database import trello_queries as tq
        from database.db import get_connection
        conn = get_connection()
        try:
            cid = cq.create_commission(conn, client_name="Sample Client", price=10)
            conn.commit()
            row = cq.get_commission(conn, cid)
            tq.create_link(conn, client_name=row["client_name"],
                           created_at=row["created_at"], card_id="C9", board_id="B1")
        finally:
            conn.close()

        r = client.post("/api/trello/unlink",
                        json={"client_name": row["client_name"],
                              "created_at": row["created_at"]})
        assert r.json()["unlinked"] is True

        conn = get_connection()
        try:
            assert cq.get_commission(conn, cid) is not None
            assert tq.get_link(conn, row["client_name"], row["created_at"]) is None
        finally:
            conn.close()


class TestNothingPersonalLeavesTheApp:
    """Commission rows carry client names and prices. The repo, the fixtures and
    every log line have to stay clean of them (FR-025, FR-027)."""

    def test_an_upstream_body_is_never_quoted(self):
        """⚠ The leak that was found in review. A 4xx from a card write echoes the
        card back, and a card's title IS the client's name -- so quoting Trello's
        body put personal data into the sync report and the log. Redacting the
        credential was not enough, because the credential was not the only secret
        in it."""
        src = open("clients/trello/client.py", encoding="utf-8").read()
        i = src.index("if r.status_code >= 400:")
        block = src[i:i + 600]
        assert "r.text" not in block, "the upstream body is back in the error"

    def test_the_scheduler_logs_counts_not_content(self):
        # The LOG CALLS only -- an earlier version of this test read the whole
        # function and matched its own comment about not logging prices, which is
        # the "anchor matched a comment" trap rather than a finding.
        src = open("polling/trello_sync.py", encoding="utf-8").read()
        calls = [l for l in src.splitlines()
                 if "logger." in l or l.strip().startswith(("c.get(", '"Trello sync:'))]
        joined = " ".join(calls)
        for leaky in ("client_name", "price", "title", "description"):
            assert leaky not in joined, f"the scheduler log can carry {leaky}"

    def test_no_module_logs_a_credential(self):
        for path in ("clients/trello/client.py", "trello/sync.py",
                     "routes/trello_api.py", "polling/trello_sync.py"):
            src = open(path, encoding="utf-8").read()
            for line in src.splitlines():
                if "logger." in line:
                    assert "token" not in line and "api_key" not in line, \
                        f"{path}: {line.strip()}"

    def test_the_client_has_no_delete_method(self):
        """⚠ Enforcement, not decoration: a method that does not exist cannot be
        called by mistake, and Trello's card delete is immediate with no undo."""
        from clients.trello.client import TrelloClient
        assert not any("delete" in n.lower() for n in dir(TrelloClient))
        src = open("clients/trello/client.py", encoding="utf-8").read()
        assert '"DELETE"' not in src


class TestBothEntryPointsStartTheScheduler:
    """⚠ Found on the 4.36.0 deploy, not by a test.

    The scheduler thread was wired into `main.py` (the desktop entry point) and
    the server was deployed, came up clean, reported the right version -- and
    never started it. `server.py` is the container's entry point and has its own
    thread table; a feature wired into one of them looks completely healthy from
    the other, because nothing fails, the thread simply is not there.

    Sync-now would still have worked through the API, which is what makes it the
    bad kind of bug: the feature appears to work and only the SCHEDULED half is
    missing, so it is discovered days later as "it does not sync on its own".
    """

    def test_the_desktop_entry_point_starts_it(self):
        src = open("main.py", encoding="utf-8").read()
        assert "run_trello_scheduler" in src

    def test_the_server_entry_point_starts_it(self):
        src = open("server.py", encoding="utf-8").read()
        assert "run_trello_scheduler" in src

    def test_it_is_in_the_servers_thread_table(self):
        """Importing the name is not starting it."""
        src = open("server.py", encoding="utf-8").read()
        i = src.index("threads = [")
        table = src[i:src.index("]", i)]
        assert "run_trello_scheduler" in table

    def test_every_scheduler_the_desktop_runs_the_server_runs_too(self):
        """The general form of the same mistake, so the next one is caught here
        rather than on a deploy."""
        main_src = open("main.py", encoding="utf-8").read()
        server_src = open("server.py", encoding="utf-8").read()
        i = server_src.index("threads = [")
        table = server_src[i:server_src.index("]", i)]
        for scheduler in ("start_posting_scheduler", "run_auto_backup_scheduler",
                          "run_trello_scheduler"):
            assert scheduler in main_src, f"{scheduler} is missing from main.py"
            assert scheduler in table, f"{scheduler} is missing from server.py's thread table"
