"""Publishing is refused to remote callers on an instance with no password (4.43.1).

On an open instance anyone on the network could publish under the operator's name —
since 4.43.0 up to 200 pieces × every site in one batch call. Every publish/schedule
endpoint is now refused to a non-loopback caller unless it presents a valid API key
(a paired desktop). Reading (GET) stays open. TestClient is not a loopback client.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import config

PUBLISH = [
    ("post", "/api/artwork/publish"), ("post", "/api/artwork/batch"), ("post", "/api/artwork/batch/plan"),
    ("post", "/api/artwork/schedule"), ("post", "/api/posting/post"), ("post", "/api/posting/update"),
    ("post", "/api/posting/queue"), ("post", "/api/posting/queue/7/reschedule"), ("post", "/api/discord/announce"),
    ("post", "/api/discord/test"), ("post", "/api/editor/stories/Some/Story/publish"),
    ("post", "/api/editor/stories/Some_Story/schedule"), ("post", "/api/editor/stories/Some_Story/drip"),
    ("post", "/api/posts/12/publish"), ("post", "/api/posts/12/schedule"), ("post", "/api/promos/3/announce"),
    ("post", "/api/masterpieces/Some_Piece/sync"), ("post", "/api/podcasts/2/episodes"),
    # PODLOCK (4.45.4): every podcast write changes the public RSS feed.
    ("post", "/api/podcasts"), ("patch", "/api/podcasts/2"), ("delete", "/api/podcasts/2"),
    ("post", "/api/podcasts/2/artwork"), ("patch", "/api/podcasts/episodes/5"),
    ("delete", "/api/podcasts/episodes/5"),
    # 4.51.0: Retry in the activity tray re-runs a publish.
    ("post", "/api/activity/abc123/retry/fa"),
    # 4.56.0 (spec 021): Retry comment posts a live reply.
    ("post", "/api/comments/7/retry"),
    # 4.56.2 (CMTTPLOPEN): a default template becomes a live reply on a later publish.
    ("put", "/api/comments/templates"),
]


@pytest.fixture
def open_app(monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    monkeypatch.setattr(config, "validate_api_key", lambda token: token == "pp_good")
    import dashboard
    return dashboard


@pytest.mark.parametrize("method,path", PUBLISH)
def test_remote_publish_is_refused_on_an_open_instance(open_app, method, path):
    r = getattr(TestClient(open_app.app, raise_server_exceptions=False), method)(
        path, **({} if method == "delete" else {"json": {}}))
    assert r.status_code == 403 and "dashboard password" in r.text


@pytest.mark.parametrize("path", ["/api/posting/queue", "/api/artwork/scheduled?name=x"])
def test_reading_stays_open(open_app, path):
    r = TestClient(open_app.app, raise_server_exceptions=False).get(path)
    assert r.status_code != 403


def test_a_paired_desktops_key_still_gets_through(open_app):
    c = TestClient(open_app.app, raise_server_exceptions=False, headers={"Authorization": "Bearer pp_good"})
    assert c.post("/api/artwork/publish", json={}).status_code == 400        # reached the route: "artwork_name is required"
    bad = TestClient(open_app.app, raise_server_exceptions=False, headers={"Authorization": "Bearer pp_bad"})
    assert bad.post("/api/artwork/publish", json={}).status_code == 403


def test_the_pattern_names_publishing_only(open_app):
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/posting/queue/clear")      # tidying, not publishing
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/posting/queue/7")
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/artwork/images")
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/artwork/publisher-notes")
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/posts/12")
    assert not open_app._PUBLISH_WHEN_OPEN.match("/api/activity/abc123/cancel")   # stopping isn't publishing


def test_a_stranger_cannot_mint_a_key_and_walk_past_the_gate(monkeypatch):
    """4.43.2 release review (HIGH): the 4.43.1 key bypass + an open key-mint route = a remote
    caller mints a key, then reads the vault. Real `validate_api_key` here — the stubbed one in
    `open_app` is exactly why the original tests missed it."""
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    remote = TestClient(dashboard.app, raise_server_exceptions=False)             # not loopback
    r = remote.post("/api/auth/api-keys", json={"name": "x"})
    assert r.status_code == 403 and "dashboard password" in r.text
    # The server's own machine can still mint one for a desktop to pair with…
    local = TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    minted = local.post("/api/auth/api-keys", json={"name": "paired desktop"}).json()
    key = minted["key"]
    try:
        # …and that key is what lets the paired desktop through, remotely.
        keyed = TestClient(dashboard.app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
        assert keyed.post("/api/artwork/publish", json={}).status_code == 400      # reached the route
        assert remote.get("/api/settings/sync").status_code == 403                # no key, no vault
    finally:
        # TESTKEYLEAK (4.45.4): a real key in the shared test settings; later tests must not see it.
        assert local.delete(f"/api/auth/api-keys/{minted['prefix']}").status_code == 200


def test_patreon_and_supporter_data_locked_when_open(open_app):
    """Spec 084: Patreon's routes and the supporter totals are refused to a remote caller on an open instance."""
    assert "/api/pat/" in open_app._SENSITIVE_WHEN_OPEN_PREFIXES
    assert "/api/supporters/" in open_app._SENSITIVE_WHEN_OPEN_PREFIXES
    c = TestClient(open_app.app, raise_server_exceptions=False)
    assert c.get("/api/supporters/summary").status_code == 403
    assert c.get("/api/pat/summary").status_code == 403


# SEC081: on an open instance, a remote caller without a paired desktop's key can't remove anything. Every DELETE is
# refused (a piece, story, post, commission or promo, and the rows: personas, tags, collections, queued posts, a
# forgotten publication, ...), and so are the two merges, which remove the absorbed piece's folder. Changing things
# stays open: editing, junk, split. The machine itself and a paired desktop's key still delete. Ids no other test uses.
DESTROY_PATHS = [("delete", "/api/artwork/images/Some_Piece"), ("delete", "/api/artwork/images/Series/Some_Piece"),
                 ("post", "/api/masterpieces/merge"), ("post", "/api/masterpieces/merge-as-variant"),
                 ("delete", "/api/editor/stories/Some_Story"), ("delete", "/api/editor/stories/Series/Some_Story"),
                 ("delete", "/api/editor/stories/Some_Story/scheduled/999999"),
                 ("delete", "/api/editor/stories/Some_Story/publication?platform=fa"),
                 ("delete", "/api/posts/999999"), ("delete", "/api/commissions/999999"),
                 ("delete", "/api/commissions/999999/files/ref.png"), ("delete", "/api/promos/999999"),
                 ("delete", "/api/personas/999999"), ("delete", "/api/tags/999999"),
                 ("delete", "/api/masterpieces/Some_Piece/variants/x"),
                 ("delete", "/api/masterpieces/Some_Piece/publication?platform=fa"),
                 ("post", "/api/posts/999999/remove-from-sites")]     # takes a journal down on its sites (4.90.0)
STILL_OPEN = [("get", "/api/artwork/images"), ("patch", "/api/artwork/images/Some_Piece"),
              ("post", "/api/masterpieces/Some_Piece/status"), ("post", "/api/masterpieces/Some_Piece/variants/x/split")]


def test_removing_anything_is_refused_remote(open_app):
    remote = TestClient(open_app.app, raise_server_exceptions=False)
    local = TestClient(open_app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    keyed = TestClient(open_app.app, raise_server_exceptions=False, headers={"Authorization": "Bearer pp_good"})
    for method, path in DESTROY_PATHS:
        kw = {} if method == "delete" else {"json": {}}
        r = getattr(remote, method)(path, **kw)
        assert r.status_code == 403 and "dashboard password" in r.text, (method, path, r.status_code)
        assert getattr(local, method)(path, **kw).status_code != 403, (method, path)
        assert getattr(keyed, method)(path, **kw).status_code != 403, (method, path)
    for method, path in STILL_OPEN:
        kw = {} if method == "get" else {"json": {}}
        assert getattr(remote, method)(path, **kw).status_code != 403, (method, path)


def test_every_delete_route_is_refused_remote(open_app):
    """However a delete route is named, one added later included."""
    remote = TestClient(open_app.app, raise_server_exceptions=False)
    deletes = [r.path for r in open_app.app.routes if "DELETE" in (getattr(r, "methods", None) or ())]
    assert len(deletes) > 40
    for path in deletes:
        r = remote.delete(re.sub(r"\{[^}]+\}", "x1", path))
        assert r.status_code == 403 and "dashboard password" in r.text, path
