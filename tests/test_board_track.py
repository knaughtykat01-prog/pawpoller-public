"""Track by artist tag and characters, not just uploader (spec 028).

e621 and Furbooru accounts can follow their artist tag and their characters as well as their own
uploads. One search covers every ticked box; each post records why it matched and whether the
account uploaded it; others' uploads either count with the account's posts or sit in the found
tables no total reads.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import config
from database.db import get_connection
from polling import board_track as bt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _conn():
    c = get_connection()
    c.execute("PRAGMA foreign_keys = OFF")
    return c


def _account(conn, platform="e621", persona=None):
    from database import accounts as accounts_db
    aid = accounts_db.get_default_account_id(conn, platform, create=True)
    conn.execute("UPDATE accounts SET persona_id = ? WHERE account_id = ?", (persona, aid))
    conn.commit()
    return aid


def _post(pid, uploader=7, artist=(), chars=(), score=1, uploader_name=""):
    return {"id": pid, "uploader_id": uploader, "uploader_name": uploader_name,
            "tags": {"artist": list(artist), "character": list(chars), "general": ["fur"]},
            "stats": {"score": {"total": score, "up": score, "down": 0}, "fav_count": 1, "comment_count": 0},
            "rating": "s", "description": "", "created_at": "2026-10-01"}


# -- queries ------------------------------------------------------------------

def test_one_e621_search_covers_every_box_and_splits_under_the_tag_limit():
    q = bt.e621_queries("me", ["inkwolf"], ["thirdfur"])
    assert q == ["~user:me ~inkwolf ~thirdfur"]
    many = bt.e621_queries("me", [], [f"c{i}" for i in range(50)])
    assert len(many) == 2 and len(many[0].split()) == bt.E621_TAG_LIMIT
    assert many[1] == " ".join("~" + f"c{i}" for i in range(37, 50))


def test_a_lone_term_has_no_or_marker():
    assert bt.e621_queries("me", [], []) == ["user:me"]


def test_furbooru_uses_philomena_or_and_quotes_odd_tags():
    assert bt.fbr_query("me", ["artist:inkwolf"], ["third fur (character)"]) == \
        'uploaded_by:me || artist:inkwolf || "third fur (character)"'


def test_tags_are_normalised_per_board_and_bad_ones_refused():
    assert bt.validate_tags("e621", ["Ink Wolf"]) == ["ink_wolf"]
    assert bt.validate_tags("fbr", ["ink_wolf"]) == ["artist:ink wolf"]
    with pytest.raises(ValueError):
        bt.validate_tags("e621", ["<script>"])
    with pytest.raises(ValueError):
        bt.validate_tags("e621", [f"t{i}" for i in range(11)])


# -- plan + reasons -------------------------------------------------------------

def test_uploads_only_is_exactly_what_polling_always_did():
    conn = _conn()
    aid = _account(conn)
    p = bt.plan(conn, "e621", aid, "me", settings={})
    assert p["queries"] is None and not p["extras"]
    assert bt.reasons("e621", {"uploader_id": "99"}, p, "me") == (["upload"], 1)


def test_my_characters_follow_the_accounts_persona():
    conn = _conn()
    aid = _account(conn, persona=3)
    conn.execute("INSERT INTO characters (character_key, name, booru_tag, persona_id) VALUES"
                 " ('thirdfur', 'ThirdFur', 'thirdfur_(character)', 3),"
                 " ('nobody', 'Nobody', '', 3), ('other', 'Other', 'other_tag', 4)")
    conn.commit()
    tagged, untagged = bt.my_characters(conn, aid)
    assert [c["key"] for c in tagged] == ["thirdfur"] and [c["name"] for c in untagged] == ["Nobody"]
    p = bt.plan(conn, "e621", aid, "me", settings={bt.KEY: {str(aid): {"characters": True}}})
    assert p["queries"] == ["~user:me ~thirdfur_(character)"]


def test_reasons_are_read_from_the_post():
    from clients.e621.client import E621Client
    c = E621Client("me", "k")
    p = {"extras": True, "artist_tags": ["inkwolf"], "char_tags": {"thirdfur": "thirdfur"}}
    mine = c._parse_post(_post(1, uploader=7, artist=["inkwolf"]))
    fan = c._parse_post(_post(2, uploader=8, artist=["inkwolf"], chars=["thirdfur"]))
    assert bt.reasons("e621", mine, p, "me", "7") == (["upload", "artist:inkwolf"], 1)
    assert bt.reasons("e621", fan, p, "me", "7") == (["artist:inkwolf", "character:thirdfur"], 0)


# -- storing ----------------------------------------------------------------------

def _plan(count_others=True):
    return {"extras": True, "artist_tags": ["inkwolf"], "char_tags": {}, "count_others": count_others}


def _detail(pid, uploader):
    from clients.e621.client import E621Client
    return E621Client("me", "k")._parse_post(_post(pid, uploader=uploader, artist=["inkwolf"]))


def test_count_no_keeps_others_out_of_the_totals_and_a_switch_moves_them_both_ways():
    conn = _conn()
    aid = _account(conn)
    bt.store(conn, "e621", _detail(1, 7), aid, _plan(False), "me", "7", "2026-10-10 01:00:00", quiet=True)
    bt.store(conn, "e621", _detail(2, 8), aid, _plan(False), "me", "7", "2026-10-10 01:00:00", quiet=True)
    conn.commit()
    main = [r[0] for r in conn.execute("SELECT submission_id FROM e621_submissions")]
    assert main == ["1"]
    assert conn.execute("SELECT COUNT(*) FROM e621_found_snapshots WHERE submission_id = '2'").fetchone()[0] == 1
    assert bt.found_summary(conn, "e621", aid)["posts"] == 1

    assert bt.move(conn, "e621", aid, to_found=False) == 1
    assert {r[0] for r in conn.execute("SELECT submission_id FROM e621_submissions")} == {"1", "2"}
    assert conn.execute("SELECT COUNT(*) FROM e621_snapshots WHERE submission_id = '2'").fetchone()[0] == 1
    assert bt.found_summary(conn, "e621", aid)["posts"] == 0
    assert bt.move(conn, "e621", aid, to_found=True) == 1     # own upload never moves
    assert [r[0] for r in conn.execute("SELECT submission_id FROM e621_submissions")] == ["1"]


def test_the_first_check_is_quiet_and_later_finds_are_stamped():
    conn = _conn()
    aid = _account(conn)
    r1 = bt.store(conn, "e621", _detail(1, 8), aid, _plan(), "me", "7", "2026-10-10 01:00:00", quiet=True)
    r2 = bt.store(conn, "e621", _detail(2, 8), aid, _plan(), "me", "7", "2026-10-10 05:00:00", quiet=False)
    r3 = bt.store(conn, "e621", _detail(3, 7), aid, _plan(), "me", "7", "2026-10-10 05:00:00", quiet=False)
    conn.commit()
    assert not r1["found_at"] and r2["found_at"] and not r3["found_at"]    # own uploads never notify
    found = bt.recent_found(conn, "2026-10-09 00:00:00")
    assert [f["submission_id"] for f in found] == ["2"]
    assert bt.describe(found[0]).startswith("New on e621: a post with your artist tag")


def test_a_post_two_accounts_track_is_stored_once_for_the_first():
    from database import accounts as accounts_db
    conn = _conn()
    a1 = _account(conn)
    a2 = accounts_db.create_account(conn, "e621", "Second")
    conn.commit()
    bt.store(conn, "e621", _detail(5, 8), a1, _plan(), "me", "7", "2026-10-10 01:00:00", quiet=True)
    r = bt.store(conn, "e621", _detail(5, 8), a2, _plan(False), "other", "9", "2026-10-10 01:00:00", quiet=False)
    conn.commit()
    assert r["shared"]
    assert conn.execute("SELECT account_id FROM e621_submissions WHERE submission_id='5'").fetchone()[0] == a1
    assert conn.execute("SELECT COUNT(*) FROM e621_snapshots WHERE submission_id='5'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM e621_found").fetchone()[0] == 0


def test_labels_are_plain_words():
    names = {"thirdfur": "ThirdFur"}
    assert bt.labels(json.dumps(["upload", "artist:inkwolf", "character:thirdfur"]), names) == [
        "your upload", "your artist tag", "your character: ThirdFur"]


# -- the e621 poll with a stand-in client ---------------------------------------------

class _FakeE621:
    calls: list = []

    def __init__(self, username="", api_key="", **kw):
        self.username = username

    def update_credentials(self, u, k):
        self.username = u

    async def close(self):
        pass

    async def validate_session(self):
        return "me"

    async def get_user_id(self):
        return "7"

    async def get_all_post_uris(self, queries=None, known=None):
        _FakeE621.calls.append(queries)
        posts = [_post(1, uploader=7)]
        if queries:
            posts.append(_post(2, uploader=8, artist=["inkwolf"]))
        return [{"post_uri": str(p["id"]), "raw": p} for p in posts]

    async def get_post_details_batch(self, items):
        from clients.e621.client import E621Client
        return [E621Client("me", "k")._parse_post(i["raw"]) for i in items]

    async def get_comments(self, sid):
        return []


def test_the_e621_poll_follows_the_artist_tag_once_ticked(monkeypatch):
    from polling import e621_poller as ep
    conn = _conn()
    aid = _account(conn)
    conn.close()
    config.save_settings({"e621_username": "me", "e621_api_key": "k"})
    monkeypatch.setattr(ep, "E621Client", _FakeE621)
    ep._e621_client = None
    _FakeE621.calls.clear()
    _run(ep.run_e621_poll_cycle(aid))
    assert _FakeE621.calls == [None]                          # uploads only: today's search
    bt.update(aid, artist_tags=["inkwolf"], saved_at="x")
    ep._e621_client = None
    _run(ep.run_e621_poll_cycle(aid))
    assert _FakeE621.calls[-1] == ["~user:me ~inkwolf"]
    conn = _conn()
    rows = {r[0]: (r[1], json.loads(r[2])) for r in conn.execute(
        "SELECT submission_id, uploaded_by_me, match_reasons FROM e621_submissions")}
    assert rows == {"1": (1, ["upload"]), "2": (0, ["artist:inkwolf"])}
    assert bt.get(aid)["user_id"] == "7" and bt.get(aid)["seeded"]
    assert conn.execute("SELECT found_at FROM e621_submissions WHERE submission_id='2'").fetchone()[0] == ""


# -- API ----------------------------------------------------------------------------------

def test_the_api_saves_choices_and_moves_rows_when_count_changes():
    from routes import board_track_api as api
    conn = _conn()
    aid = _account(conn)
    bt.store(conn, "e621", _detail(2, 8), aid, _plan(), "me", "7", "2026-10-10 01:00:00", quiet=True)
    conn.commit()
    conn.close()
    assert api.get_track(aid)["saved"] is False
    r = api.put_track(aid, {"artist_tags": ["Ink Wolf"], "characters": False, "count_others": False})
    assert r["moved"] == 1
    t = api.get_track(aid)
    assert t["saved"] and t["settings"]["artist_tags"] == ["ink_wolf"] and t["found"]["posts"] == 1
    with pytest.raises(Exception):
        api.put_track(aid, {"artist_tags": ["<b>"]})
    with pytest.raises(Exception):
        api.get_track(999999)
