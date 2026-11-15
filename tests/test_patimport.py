"""Spec 087: a creator's existing Patreon picture posts come into the Library.

A made-up page (tests/patimport_fixtures.py); nothing reaches Patreon. Sections: the read and the found list, import,
the download rules, the audience, the pace.
"""
from __future__ import annotations

import inspect
import json
import logging

import pytest

import config
from clients.pat import account as pat_account
from clients.pat import client as patc
from clients.pat import pictures
from database import accounts as adb
from database.db import get_connection
from polling import fa_standdown, pat_poller
from posting import artwork_importer, artwork_reader, pat_import
from posting.platforms.base import NEEDS_RATING, PAT_PICK_NEEDED
from tests.patimport_fixtures import PIC_HOST, ImportPatreon, public_dns
from tests.test_patreon import make_account, run


async def _no_sleep(*_a, **_k):
    return None


@pytest.fixture
def page(tmp_path, monkeypatch):
    """A polled made-up page: (stand-in, account id). The Library is an empty temp folder."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patc, "MIN_GAP_S", 0)
    pat_account._checked.clear()
    art = tmp_path / "art"
    art.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: art)
    fake, aid = ImportPatreon(), make_account()
    monkeypatch.setattr(pat_poller, "_transport", fake.transport())
    monkeypatch.setattr(pictures, "_transport", fake.transport())
    monkeypatch.setattr(pat_import, "_sleep", _no_sleep)
    monkeypatch.setattr(pat_import, "_last", 0.0)
    public_dns(monkeypatch)
    fa_standdown.clear("pat")
    run(pat_poller.run_pat_poll_cycle(aid))
    return fake, aid


def _rows():
    conn = get_connection()
    try:
        return {r["submission_id"]: dict(r) for r in conn.execute("SELECT * FROM pat_submissions")}
    finally:
        conn.close()


def _found():
    from routes.submissions_api import get_discovered_unlinked
    conn = get_connection()
    try:
        return {i["submission_id"]: i for i in get_discovered_unlinked(conn, platform_filter="pat")}
    finally:
        conn.close()


# ── the read and the found list ────────────────────────────────────────────────────────────────────────────

def test_fields_asked_are_the_fixed_list_and_no_text(page):
    fake, _ = page
    q = fake.calls("GET", "/api/posts")[0]["query"]
    assert q["fields[post]"] == [patc.POST_LIST_FIELDS] and "content" not in patc.POST_LIST_FIELDS
    assert q["include"] == [patc.POST_LIST_INCLUDE]
    asked = " ".join(v for vals in q.values() for v in vals)
    assert not any(w in asked for w in ("user", "member", "pledge", "content"))


def test_kind_pictures_and_audience_stored(page):
    rows = _rows()
    assert (rows["801"]["post_type"], rows["801"]["has_pictures"], rows["801"]["audience"]) == ("image_file", 1, "public")
    assert rows["802"]["audience"] == "tiers" and json.loads(rows["802"]["tier_ids"]) == ["40"]
    assert rows["803"]["audience"] == "patrons"
    assert rows["804"]["audience"] == "other" and json.loads(rows["804"]["tier_ids"]) == []
    assert rows["806"]["has_pictures"] == 0 and rows["807"]["has_pictures"] == 0
    for r in rows.values():         # no picture address and no post text is ever stored
        assert not r["thumbnail_url"] and not r["description"] and "patreonusercontent" not in json.dumps(r)


def test_found_list_lists_picture_posts_only(page):
    found = _found()
    assert {k for k, v in found.items() if v["kind"] == "art"} == {"801", "802", "803", "804", "805"}
    assert found["806"]["kind"] != "art" and found["807"]["kind"] != "art"
    assert found["801"]["audience_label"] == "Everyone" and found["803"]["audience_label"] == "All paying members"
    assert found["802"]["audience_label"] == "Sketch club"          # the tier's name, markup removed
    assert found["804"]["audience"] == "other" and found["804"]["audience_label"] == "Members"   # never "Everyone"
    assert found["801"]["has_pictures"] is True and found["801"]["thumbnail_url"] == ""


def test_found_list_makes_no_request(page):
    fake, _ = page
    before = len(fake.requests)
    _found()
    assert len(fake.requests) == before


def test_never_post_account_not_listed_and_import_refused_before_any_request(page, monkeypatch):
    fake, aid = page
    monkeypatch.setattr(adb, "never_post_ids", lambda settings=None: {aid})
    assert _found() == {}
    before = len(fake.requests)
    with pytest.raises(ValueError, match="Never post"):
        artwork_importer.import_artwork("pat", "801")
    assert len(fake.requests) == before, "refused before any request to Patreon"


def test_patreon_post_never_linked_to_a_story_by_title():
    from posting import sync
    assert sync.PLATFORM_TABLES["pat"]["no_title_link"] is True
    src = inspect.getsource(sync)
    assert 'cfg.get("no_title_link")' in src, "the title scan skips Patreon"


def test_post_importer_refuses_pat(page):
    from posting import post_importer
    with pytest.raises(ValueError):
        post_importer.import_post("pat", "806")


def test_unknown_audience_kind_is_never_public():
    assert pat_import.audience([{"kind": "made_up_kind", "tier_id": ""}], {"40"}) is None
    assert pat_import.audience([{"kind": "public", "tier_id": ""}, {"kind": "tier", "tier_id": "40"}], {"40"}) is None
    assert pat_import.audience([], {"40"}) is None
    assert pat_import.stored([{"kind": "min_cents_pledged", "tier_id": ""}], set()) == {"audience": "other", "tier_ids": []}
    assert pat_import.label("other", [], []) == "Members" and pat_import.label("tiers", ["9"], []) == "Members"


# ── import ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_import_makes_a_piece(page):
    fake, aid = page
    res = artwork_importer.import_artwork("pat", "801")
    assert res["status"] == "imported" and res["images"] == 1 and res["errors"] == []
    art = artwork_reader.load_artwork(res["name"])
    raw = artwork_reader.read_raw_metadata(res["name"])
    assert art.title == "Harbour Lights" and art.description == "Out on the water."        # plain text
    assert art.image == "image.png" and (art.path / "image.png").read_bytes()[:4] == b"\x89PNG"
    assert art.original_posted_at.startswith("2026-10-02") and "pat" in art.platforms
    assert raw["import_source"]["url"] == "https://www.patreon.com/posts/801"
    conn = get_connection()
    try:
        pub = conn.execute("SELECT * FROM publications WHERE platform='pat' AND external_id='801'").fetchone()
    finally:
        conn.close()
    assert pub and pub["story_name"] == res["name"] and pub["account_id"] == aid
    assert "801" not in _found(), "an imported post leaves the found list"
    assert artwork_importer.import_artwork("pat", "801")["status"] == "already_imported"
    assert fake.writes() == [], "import and the read send GET requests only"


def test_a_posts_words_come_from_either_field_as_plain_text(page):
    """Found on a real page: the words arrive in the editor's own document and `content` is empty. Both shapes
    give the same plain description; a picture inside the text contributes nothing (no address reaches the piece)."""
    from clients.pat import editor_json
    fake, _ = page
    a = artwork_reader.load_artwork(artwork_importer.import_artwork("pat", "801")["name"])
    assert a.description == "Out on the water."
    fake.html_content = True
    b = artwork_reader.load_artwork(artwork_importer.import_artwork("pat", "802")["name"])
    assert b.description == "For the club."
    assert "patreonusercontent" not in a.description + b.description
    asked = fake.calls("GET", "/api/posts/801")[0]["query"]["fields[post]"][0]
    assert asked == patc.POST_ONE_FIELDS and "content_json_string" in asked
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "One "}, {"type": "text", "text": "line", "marks": [{"type": "bold"}]}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a point"}]}]}]},
        {"type": "image", "attrs": {"src": "https://c10.patreonusercontent.com/x.png"}},
        {"type": "paragraph", "content": [{"type": "text", "text": "<b>not markup</b>"}]}]}
    import json as _json
    assert editor_json.plain_from_doc(_json.dumps(doc), 5000) == "One line\na point\n<b>not markup</b>"
    assert editor_json.plain_from_doc(_json.dumps(doc), 6) == "One li"
    for bad in (None, "", "not json", "[]", "{}"):
        assert editor_json.plain_from_doc(bad, 100) == ""


@pytest.mark.parametrize("nsfw", [False, True])
def test_import_sets_no_rating(nsfw, page, monkeypatch):
    """Patreon gives no rating for a post: nothing is guessed from the page's 18+ mark or the default rating."""
    fake, aid = page
    pat_account.record_campaign(aid, {"campaign_id": "111", "is_nsfw": nsfw})
    base = dict(config.get_settings())
    monkeypatch.setattr(config, "get_settings", lambda: {**base, "artwork_default_rating": "general"})
    name = artwork_importer.import_artwork("pat", "801")["name"]
    art = artwork_reader.load_artwork(name)
    assert art.rating == "" and art.needs_rating
    pkg = artwork_reader.build_artwork_package(art, "fa")
    from posting.manager import _get_poster
    assert _get_poster("fa").refusal(pkg) == NEEDS_RATING


def test_a_picture_that_did_not_arrive_is_asked_for_once_more(page):
    """A download that drops, or an address whose time ran out: the post is asked for once more and only the
    missing picture is tried again. One that still fails is named and the rest come in (FR-006)."""
    fake, _ = page
    fake.fail_first = {"8052"}                  # fails once, then arrives
    res = artwork_importer.import_artwork("pat", "805")
    assert res["images"] == 3 and res["errors"] == []
    assert len(fake.calls("GET", "/api/posts/805")) == 2, "the post was asked for once more, not more"
    assert len([c for c in fake.picture_calls() if "/8052/" in c["path"]]) == 2
    assert len([c for c in fake.picture_calls() if "/8051/" in c["path"]]) == 1, "only the missing one is retried"


def test_a_picture_that_never_arrives_is_named_and_the_rest_come_in(page):
    fake, _ = page
    fake.picture_status_for = {"8052": 500}
    res = artwork_importer.import_artwork("pat", "805")
    assert res["images"] == 2 and res["errors"] == [{"picture": 2, "error": pat_import.PICTURE_FAILED}]
    assert len([c for c in fake.picture_calls() if "/8052/" in c["path"]]) == 2, "tried twice, then a plain failure"


def test_when_no_picture_arrives_nothing_is_made_and_the_post_stays(page):
    fake, _ = page
    fake.picture_status = 500
    with pytest.raises(ValueError, match="couldn't be downloaded"):
        artwork_importer.import_artwork("pat", "805")
    assert artwork_reader.list_artworks() == [] and "805" in _found()
    fake.picture_status = 200
    assert artwork_importer.import_artwork("pat", "805")["images"] == 3


def test_one_posts_pictures_are_capped_in_memory(page, monkeypatch):
    monkeypatch.setattr(pat_import, "POST_BYTES", 100)
    with pytest.raises(pat_import.PatImportError, match="larger than PawPoller brings in at once"):
        artwork_importer.import_artwork("pat", "805")
    assert artwork_reader.list_artworks() == [] and "805" in _found()


def test_marks_survive_a_merge(page, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes.masterpieces_api import masterpieces_router
    app = FastAPI()
    app.include_router(masterpieces_router)
    client = TestClient(app)
    paid = artwork_importer.import_artwork("pat", "802")["name"]
    odd = artwork_importer.import_artwork("pat", "804")["name"]
    older = artwork_reader.create_artwork(title="Older One", image_filename="a.png", image_bytes=b"\x89PNG fake",
                                          rating="general", tags={"default": ["sample"]})
    for n in (paid, odd):
        artwork_reader.set_rating(n, "general")
    assert client.post("/api/masterpieces/merge", json={"keep": older, "drop": paid}).status_code == 200
    assert artwork_reader.marks_of(older) == {"pat_paid": True}
    r = client.post("/api/masterpieces/merge-as-variant", json={"keep": older, "absorb": odd, "key": "alt"})
    assert r.status_code == 200, r.text
    assert artwork_reader.marks_of(older) == {"pat_paid": True, "pat_pick_needed": True,
                                              "pat_pick_note": pat_import.PICK_NOTE}


def test_the_upgrade_adds_the_columns_to_a_4_90_database():
    import sqlite3

    from database import pat_queries
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE pat_submissions (submission_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL DEFAULT 0,
        portal TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', description TEXT DEFAULT '',
        username TEXT NOT NULL DEFAULT '', rating TEXT DEFAULT '', genre TEXT DEFAULT '', link TEXT DEFAULT '',
        thumbnail_url TEXT DEFAULT '', posted_at TEXT, audience TEXT NOT NULL DEFAULT '', views INTEGER DEFAULT 0,
        favorites_count INTEGER DEFAULT 0, comments_count INTEGER DEFAULT 0, downloads_count INTEGER DEFAULT 0,
        votes INTEGER DEFAULT 0, score REAL DEFAULT 0, updated_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("INSERT INTO pat_submissions (submission_id, title) VALUES ('1', 'Sample Story')")
    pat_queries.ensure_pat_columns(conn)
    pat_queries.ensure_pat_columns(conn)                 # twice: it only adds what is missing
    old = dict(conn.execute("SELECT * FROM pat_submissions WHERE submission_id = '1'").fetchone())
    assert (old["post_type"], old["has_pictures"], old["tier_ids"]) == ("", 0, "[]")
    pat_queries.upsert_pat_submission(conn, {"submission_id": "2", "title": "t", "post_type": "image_file",
                                             "has_pictures": True, "audience": "tiers", "tier_ids": ["40"]}, 0)
    new = dict(conn.execute("SELECT * FROM pat_submissions WHERE submission_id = '2'").fetchone())
    assert (new["post_type"], new["has_pictures"], new["tier_ids"]) == ("image_file", 1, '["40"]')


def test_several_pictures_make_several_pieces_and_one_bad_one_does_not_stop_the_rest(page):
    fake, _ = page
    fake.bad_pictures = {"8052"}
    res = artwork_importer.import_artwork("pat", "805")
    assert res["images"] == 2 and len(res["names"]) == 2
    assert res["errors"] == [{"picture": 2, "error": pat_import.NOT_A_PICTURE}]
    titles = sorted(artwork_reader.load_artwork(n).title for n in res["names"])
    assert titles == ["Three Studies (1/3)", "Three Studies (3/3)"]


def test_other_page_and_post_of_another_page_refused(page):
    fake, _ = page
    for camp in ("999", ""):            # Patreon's own answer names another page, or none
        fake.post_campaign = camp
        with pytest.raises(ValueError, match="isn't on this Patreon page"):
            artwork_importer.import_artwork("pat", "801")
    fake.post_campaign = None
    fake.campaign = "222"               # the sign-in is now for a different page than the saved one
    with pytest.raises(ValueError, match="different page"):
        artwork_importer.import_artwork("pat", "801")
    assert artwork_reader.list_artworks() == [] and fake.picture_calls() == []


def test_uses_the_rows_account(page, monkeypatch):
    fake, aid = page
    seen = []
    real = config.resolve_account_credentials
    monkeypatch.setattr(config, "resolve_account_credentials",
                        lambda site, account_id, *a, **k: seen.append((site, account_id)) or real(site, account_id, *a, **k))
    artwork_importer.import_artwork("pat", "801")
    assert seen == [("pat", aid)]


@pytest.mark.parametrize("bad", ["80x", "801/../x", "", "-1", "8" * 30])
def test_bad_id_refused(bad, page):
    fake, _ = page
    before = len(fake.requests)
    with pytest.raises(ValueError):
        artwork_importer.import_artwork("pat", bad)
    assert len(fake.requests) == before


def test_unknown_id_and_pictureless_post_refused(page):
    fake, _ = page
    before = len(fake.requests)
    with pytest.raises(ValueError, match="not found"):
        artwork_importer.import_artwork("pat", "999999")
    with pytest.raises(ValueError, match="no pictures"):
        artwork_importer.import_artwork("pat", "806")
    assert len(fake.requests) == before


def test_promote_route_gets_the_same_checks(page):
    from database import masterpiece_queries as mq
    conn = get_connection()
    try:
        out = mq.promote_from_submission(conn, "pat", "801")
        conn.commit()
    finally:
        conn.close()
    assert artwork_reader.load_artwork(out["name"]).needs_rating


def test_signed_out_and_bot_check_say_so_and_leave_nothing(page):
    fake, _ = page
    fake.status_override = ("/api/current_user", 401, b"{}", {})
    with pytest.raises(pat_import.PatImportError) as e:
        artwork_importer.import_artwork("pat", "801")
    assert str(e.value) == pat_import.SIGN_IN and e.value.stop
    fake.status_override = ("/api/current_user", 403, b"Just a moment", {"cf-mitigated": "challenge"})
    with pytest.raises(pat_import.PatImportError) as e:
        artwork_importer.import_artwork("pat", "801")
    assert "waiting" in str(e.value) and e.value.stop and fa_standdown.remaining("pat") > 0
    assert artwork_reader.list_artworks() == []


def test_post_gone_leaves_the_found_list(page):
    fake, _ = page
    fake.one_post_status = 404
    assert "801" in _found()
    with pytest.raises(ValueError, match="no longer on Patreon"):
        artwork_importer.import_artwork("pat", "801")
    assert "801" not in _found() and "802" in _found(), "the gone post leaves the list; the others stay"


def test_import_creates_no_share_feed_or_collection(page):
    name = artwork_importer.import_artwork("pat", "801")["name"]
    conn = get_connection()
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        hits = []
        for t in tables:
            if t in ("publications", "masterpieces", "pat_submissions", "pat_snapshots", "pat_poll_log", "image_hashes"):
                continue
            try:
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            except Exception:
                continue
            if any(name in json.dumps([str(v) for v in tuple(r)]) for r in rows):
                hits.append(t)
    finally:
        conn.close()
    assert hits == [], f"import wrote the piece into: {hits}"


def test_import_all_includes_patreon_is_repeatable_and_capped(page, monkeypatch):
    from fastapi import HTTPException

    from routes import artwork_api
    monkeypatch.setattr(pat_import, "RUN_CAP", 2)
    first = artwork_api.import_all_discovered_art()
    assert first["imported"] == 2 and first["pat_left"] == 3
    monkeypatch.setattr(pat_import, "RUN_CAP", 200)
    second = artwork_api.import_all_discovered_art()
    assert second["imported"] == 3 and "pat_left" not in second
    third = artwork_api.import_all_discovered_art()
    assert third["imported"] == 0 and third["total_art"] == 0
    assert artwork_api._IMPORT_RUN.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as e:
            artwork_api.import_all_discovered_art()
        assert e.value.status_code == 409
    finally:
        artwork_api._IMPORT_RUN.release()


def test_bot_check_ends_the_patreon_part(page):
    from routes import artwork_api
    fake, _ = page
    fake.status_override = ("/api/current_user", 429, b"slow", {})
    out = artwork_api.import_all_discovered_art()
    assert out["imported"] == 0 and out["failed"] == 1 and out["pat_not_tried"] == 4
    assert "waiting" in out["pat_stopped"], "the screen is given the reason the Patreon part stopped"
    assert len(fake.calls("GET", "/api/current_user")) == 2, "one try in the run (plus the poll's), not five"


# ── the download rules ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://evil.example/x.png", "https://patreonusercontent.com.evil.example/x.png",
    "https://evilpatreonusercontent.com/x.png", "http://c10.patreonusercontent.com/x.png",
    "https://c10.patreonusercontent.com:8443/x.png", "https://user:@c10.patreonusercontent.com/x.png",
    "https://93.184.216.34/x.png", "ftp://c10.patreonusercontent.com/x.png", "", "not an address"])
def test_wrong_host_scheme_port_or_user_refused(url, page):
    fake, _ = page
    assert not pictures.host_ok(url)
    with pytest.raises(pictures.PictureError, match="couldn't be downloaded"):
        pictures.fetch(url)
    assert fake.picture_calls() == []


def test_redirect_refused_and_nothing_of_the_sign_in_is_sent(page):
    fake, _ = page
    url = fake.picture_url("8011")
    data, ext = pictures.fetch(url)
    assert ext == ".png" and data[:4] == b"\x89PNG"         # by its bytes: the stand-in declares text/html
    sent = fake.picture_calls()[-1]["headers"]
    assert "cookie" not in {k.lower() for k in sent} and "x-csrf-signature" not in {k.lower() for k in sent}
    assert "PLANTED" not in json.dumps(sent)
    fake.picture_location = "https://c10.patreonusercontent.com/elsewhere.png"
    n = len(fake.picture_calls())
    with pytest.raises(pictures.PictureError, match="couldn't be downloaded"):
        pictures.fetch(url)
    assert len(fake.picture_calls()) == n + 1, "the redirect was not followed"


def test_too_large_stopped_and_non_picture_refused(page, monkeypatch):
    fake, _ = page
    url = fake.picture_url("8011")
    fake.picture_body = b"<html>not a picture</html>"
    with pytest.raises(pictures.PictureError, match="isn't a picture"):
        pictures.fetch(url)
    fake.picture_body = None
    monkeypatch.setattr(pictures, "MAX_BYTES", 10)
    with pytest.raises(pictures.PictureError, match="larger than the Library takes"):
        pictures.fetch(url)


def test_marked_address_appears_nowhere(page, caplog):
    """A failing download: the signed address is in no reply, log line (httpx's own included), file or table."""
    fake, _ = page
    fake.bad_pictures = {"8052"}
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.INFO)
        res = artwork_importer.import_artwork("pat", "805")
        with pytest.raises(ValueError) as e:
            fake.picture_status = 500
            artwork_importer.import_artwork("pat", "802")
    blob = json.dumps(res) + str(e.value) + repr(e.value.__cause__) + repr(e.value.__context__)
    blob += "\n".join(r.getMessage() for r in caplog.records)
    for n in res["names"]:
        blob += json.dumps(artwork_reader.read_raw_metadata(n))
    conn = get_connection()
    try:
        for t in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            try:
                blob += json.dumps([[str(v) for v in tuple(r)] for r in conn.execute(f"SELECT * FROM {t}")])
            except Exception:
                pass
    finally:
        conn.close()
    assert "SIGNED_" not in blob and "token-hash" not in blob and PIC_HOST not in blob
    assert [x["error"] for x in res["errors"]] == [pat_import.NOT_A_PICTURE] and "couldn't be downloaded" in str(e.value)


# ── the audience and the paid mark ─────────────────────────────────────────────────────────────────────────

def _poster_refusal(name, extra=None):
    from posting.platforms.patreon import PatreonPoster
    art = artwork_reader.load_artwork(name)
    pkg = artwork_reader.build_artwork_package(art, "pat", rating_override="general")
    pkg.extra.update(extra or {})                       # what the manager merges in from per-send / batch options
    pkg.needs_rating = False                            # look past the rating hold at the audience hold
    return pkg, PatreonPoster().refusal(pkg, {"is_nsfw": False, "default_audience": {"audience": "public"}, "tiers": []})


def test_known_audience_is_saved_and_only_audience_and_tiers(page):
    name = artwork_importer.import_artwork("pat", "802")["name"]
    raw = artwork_reader.read_raw_metadata(name)
    assert raw["categories"]["pat"] == {"audience": "tiers", "tier_ids": ["40"]}
    assert raw["pat_paid"] is True and "pat_pick_needed" not in raw
    pub = artwork_reader.read_raw_metadata(artwork_importer.import_artwork("pat", "801")["name"])
    assert pub["categories"]["pat"] == {"audience": "public", "tier_ids": []} and "pat_paid" not in pub
    members = artwork_reader.read_raw_metadata(artwork_importer.import_artwork("pat", "803")["name"])
    assert members["categories"]["pat"]["audience"] == "patrons" and members["pat_paid"] is True


def test_removed_tier_saves_nothing(page):
    fake, aid = page
    pat_account.record_campaign(aid, {"campaign_id": "111", "is_nsfw": False}, [])     # the tier is gone from the page
    raw = artwork_reader.read_raw_metadata(artwork_importer.import_artwork("pat", "802")["name"])
    assert "pat" not in (raw.get("categories") or {}) and raw["pat_pick_needed"] is True and raw["pat_paid"] is True


def test_unknown_audience_saves_nothing_and_send_refused_until_picked(page):
    """Through the real package build: the account default "Everyone", a per-send audience and a batch-wide one
    can't fill in for a piece whose audience couldn't be matched."""
    name = artwork_importer.import_artwork("pat", "804")["name"]
    raw = artwork_reader.read_raw_metadata(name)
    assert "pat" not in (raw.get("categories") or {}) and raw["pat_pick_needed"] is True
    assert raw["pat_pick_note"] == pat_import.PICK_NOTE and raw["pat_paid"] is True
    for extra in (None, {"audience": "public"}, {"audience": "patrons", "tier_ids": []}):
        pkg, refusal = _poster_refusal(name, extra)
        assert pkg.pat_pick_needed is True and refusal == PAT_PICK_NEEDED, extra
    # Saving an audience on the piece itself is what lifts it.
    artwork_reader.save_artwork_metadata(name, {"categories": {"pat": {"audience": "patrons", "tier_ids": []}}})
    pkg, refusal = _poster_refusal(name)
    assert pkg.pat_pick_needed is False and refusal != PAT_PICK_NEEDED
    assert artwork_reader.marks_of(name).get("pat_pick_needed") is True, "the mark itself is never edited away"


def test_marks_survive_a_details_save(page):
    name = artwork_importer.import_artwork("pat", "804")["name"]
    artwork_reader.save_artwork_metadata(name, {"title": "Renamed", "pat_paid": False, "pat_pick_needed": False,
                                                "needs_rating": False, "rating": "general"})
    assert artwork_reader.marks_of(name) == {"needs_rating": True, "pat_paid": True, "pat_pick_needed": True,
                                             "pat_pick_note": pat_import.PICK_NOTE}


def test_paid_mark_in_batch_plan_and_notice_does_not_block(page):
    from routes import artwork_api
    paid = artwork_importer.import_artwork("pat", "802")["name"]
    free = artwork_importer.import_artwork("pat", "801")["name"]
    artwork_reader.set_rating(paid, "general")
    artwork_reader.set_rating(free, "general")
    out = artwork_api.batch_plan({"names": [paid, free], "platforms": ["bsky"], "gap_minutes": 0})
    assert out["notices"] == [{"name": paid, "title": "Tea on the Roof", "notice": artwork_api.PAID_NOTICE}]
    from posting import batch
    bare = batch.plan([paid, free], **artwork_api._batch_args({"platforms": ["bsky"], "gap_minutes": 0}))
    assert set(out) - {"notices"} == set(bare) and out["totals"] == bare["totals"], "a notice changes nothing else"
    assert [(x["name"], [s["platform"] for s in x["posts"]]) for x in out["pieces"]] == \
        [(x["name"], [s["platform"] for s in x["posts"]]) for x in bare["pieces"]]


# ── the pace ───────────────────────────────────────────────────────────────────────────────────────────────

def test_standdown_checked_before_each_request(page):
    fake, _ = page
    fa_standdown.note_challenge("pat")
    before = len(fake.requests)
    with pytest.raises(pat_import.PatImportError) as e:
        artwork_importer.import_artwork("pat", "801")
    assert e.value.stop and "waiting" in str(e.value) and len(fake.requests) == before


def test_picture_host_slow_down_starts_standdown_and_a_403_does_not(page):
    fake, _ = page
    fake.picture_status = 403                   # an address whose time ran out is a 403 too: one picture, no pause
    with pytest.raises(ValueError, match="couldn't be downloaded") as e:
        artwork_importer.import_artwork("pat", "801")
    assert not getattr(e.value, "stop", False) and fa_standdown.remaining("pat") == 0
    fake.picture_status = 429
    with pytest.raises(pat_import.PatImportError) as e:
        artwork_importer.import_artwork("pat", "801")
    assert e.value.stop and fa_standdown.remaining("pat") > 0 and artwork_reader.list_artworks() == []


def test_one_pace_across_requests_and_pictures(page, monkeypatch):
    slept = []

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(pat_import, "_sleep", sleep)
    monkeypatch.setattr(patc, "MIN_GAP_S", 5)
    monkeypatch.setattr(patc.PatClient, "_pace", _no_sleep)         # the client's own per-object pace aside
    artwork_importer.import_artwork("pat", "805")                   # page check, the post, three pictures
    artwork_importer.import_artwork("pat", "801")                   # a second post in the same run: same clock
    assert len(slept) >= 7 and all(0 < s <= 5 for s in slept)


def test_never_beside_a_read_waits_then_says_busy(page, monkeypatch):
    fake, _ = page
    monkeypatch.setattr(pat_import, "LOCK_WAIT", 0.05)
    assert pat_poller._lock.acquire(blocking=False)
    try:
        before = len(fake.requests)
        with pytest.raises(pat_import.PatImportError) as e:
            artwork_importer.import_artwork("pat", "801")
        assert str(e.value) == pat_import.BUSY and e.value.stop and len(fake.requests) == before
    finally:
        pat_poller._lock.release()
    assert artwork_importer.import_artwork("pat", "801")["status"] == "imported"
    assert not pat_poller._lock.locked(), "the import gave the lock back"
