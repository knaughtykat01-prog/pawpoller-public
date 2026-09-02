"""DeviantArt stored the wrong one of its two ids (3.29.0).

Reported as: posting through an item still asks you to link that same post up,
on DeviantArt and FurAffinity.

**DA has two identifiers for one deviation and they are not interchangeable.**
The integer at the end of every public URL, and the GUID the API calls
`deviationid`. `clients/da/client.py` states the contract in its own header —
*"Deviation IDs stored in the DB are integers (parsed from the deviation URL);
the API's UUID `deviationid` is used only transiently for metadata calls"* — and
the poller, the image hashes, the publications and the Masterpiece members all
key on the integer.

The poster stored the GUID. So a piece published to DA through PawPoller wrote
an id that joined to nothing it had ever polled: the auto-link in
`manager.post_artwork` minted a member matching no submission, and the piece
kept offering its own upload back under *"is this the same image?"*. One fact,
two declarations, no check — measured live at three of six DA members and two
publications in that state, one of them carrying **both** a good integer member
and a stray GUID one for the same upload.

The FurAffinity half was not an id bug at all. FA ids matched; what was missing
was the suggestion scan knowing that a hash already belongs to something we
published. Covered by `test_suggestions_skip_our_own_posts` below.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from database import masterpiece_queries as mq
from database.db import _da_int_id, _da_rekey, get_connection

GUID = "3D63F235-A433-129F-A5A2-DD0BCCD53BEC"
URL = "https://www.deviantart.com/someone/art/Sitting-Serious-1371636392"


# ── the id that gets stored ──────────────────────────────────────────

class _FakeClient:
    """Just enough DAClient for the poster's create/edit paths."""

    def __init__(self, create=None, update=None):
        self.target_user = "someone"
        self._gallery_cache: dict[int, dict] = {}
        self._create = create or {}
        self._update = update or {}
        self.updated_with: list = []
        self.gallery_calls = 0

    async def oauth_create_literature(self, **kw):
        return self._create

    async def oauth_update_literature(self, deviation_id, **kw):
        self.updated_with.append(deviation_id)
        return self._update

    async def get_all_deviation_ids(self):
        self.gallery_calls += 1
        return []

    # the real method under test, lifted verbatim by delegation
    async def uuid_for(self, deviation_id):
        from clients.da.client import DAClient
        return await DAClient.uuid_for(self, deviation_id)


def _poster(client):
    from posting.platforms.deviantart import DeviantArtPoster
    p = DeviantArtPoster()

    async def _ensure():
        return client, "tok"

    p._ensure_client = _ensure
    return p


def _package(tmp_path):
    from posting.platforms.base import StoryUploadPackage
    f = tmp_path / "body.txt"
    f.write_text("body", encoding="utf-8")
    return StoryUploadPackage(
        platform="da", story_name="Sample_Story", chapter_index=0, chapter_title="",
        title="Sitting Serious", description="d", tags=["a"], rating="general",
        file_path=str(f), file_type="txt",
    )


def test_post_stores_the_integer_from_the_url_not_the_guid(tmp_path):
    """THE regression. The GUID joins to nothing the poller ever wrote."""
    c = _FakeClient(create={"deviationid": GUID, "url": URL})
    r = asyncio.run(_poster(c).post(_package(tmp_path)))
    assert r.success
    assert r.external_id == "1371636392", \
        f"stored {r.external_id!r} — anything non-numeric matches no da_submissions row"
    assert r.external_url == URL


def test_post_falls_back_to_the_guid_but_says_so(tmp_path, caplog):
    """A dangling id beats no id, but it will misbehave the same way, so it
    must not be silent."""
    c = _FakeClient(create={"deviationid": GUID, "url": "https://www.deviantart.com/x/art/no-id-here"})
    with caplog.at_level("WARNING"):
        r = asyncio.run(_poster(c).post(_package(tmp_path)))
    assert r.external_id == GUID
    assert any("GUID" in m or "integer id" in m for m in caplog.messages)


@pytest.mark.parametrize("url,expected", [
    ("https://www.deviantart.com/u/art/Some-Title-1351251437", "1351251437"),
    ("https://www.deviantart.com/u/art/Some-Title-1351251437/", "1351251437"),
    ("https://www.deviantart.com/u/art/T-123?x=1", "123"),
    ("https://www.deviantart.com/u/art/no-trailing-id", ""),
    ("", ""),
])
def test_the_integer_is_parsed_out_of_every_url_shape(url, expected):
    assert _da_int_id(url) == expected


# ── writing still works, because the API wants the other one ─────────

def test_edit_converts_the_integer_to_the_guid_the_api_wants(tmp_path):
    """Storing the integer is only safe if the write path converts back —
    `/deviation/literature/update/{id}` takes the GUID. Without this the fix
    would trade a linking bug for a broken edit."""
    c = _FakeClient(update={"url": URL})
    c._gallery_cache[1371636392] = {"uuid": GUID, "url": URL}
    r = asyncio.run(_poster(c).edit("1371636392", _package(tmp_path)))
    assert r.success
    assert c.updated_with == [GUID], \
        f"the update endpoint was called with {c.updated_with!r}, not the GUID"
    assert r.external_id == "1371636392", "the stored id must stay the integer"


def test_a_legacy_guid_row_still_edits(tmp_path):
    """Rows written before this fix, and other installs that have not migrated,
    hold the GUID. `uuid_for` passes it through rather than failing."""
    c = _FakeClient(update={"url": URL})
    r = asyncio.run(_poster(c).edit(GUID, _package(tmp_path)))
    assert r.success
    assert c.updated_with == [GUID]
    assert c.gallery_calls == 0, "a GUID needs no lookup — that would be a wasted fetch"


def test_uuid_for_warms_a_cold_cache_once():
    c = _FakeClient()
    assert asyncio.run(c.uuid_for("999")) == ""
    assert c.gallery_calls == 1, "a cold cache must be filled before giving up"


def test_uuid_for_is_empty_for_an_empty_id():
    c = _FakeClient()
    assert asyncio.run(c.uuid_for("")) == ""
    assert asyncio.run(c.uuid_for(None)) == ""


# ── the URL an edit writes back ──────────────────────────────────────

def test_edit_no_longer_invents_a_url_that_was_never_valid():
    """It used to build `/{user}/art/{external_id}`. Real deviation URLs are
    `/{user}/art/{slug}-{id}`, so every edit overwrote a working link with a
    404."""
    from posting.platforms.deviantart import DeviantArtPoster
    c = _FakeClient()
    assert DeviantArtPoster._deviation_url(c, {"url": URL}, "1371636392") == URL

    c._gallery_cache[1371636392] = {"uuid": GUID, "url": URL}
    assert DeviantArtPoster._deviation_url(c, {}, "1371636392") == URL, \
        "should fall back to the cached URL before giving up"

    assert DeviantArtPoster._deviation_url(_FakeClient(), {}, "1371636392") == "", \
        "an unknown URL must be empty — manager.update_story reads that as 'keep what is stored'"


# ── the repair ───────────────────────────────────────────────────────

def _seed_da(conn):
    """The exact live shape: a GUID publication, a GUID member on a piece with
    no integer member, and a GUID member on a piece that already has one."""
    conn.executescript("""
        INSERT INTO publications (story_name, chapter_index, platform, external_id,
                                  external_url, status, account_id, content_type)
        VALUES ('Sitting_Serious', 0, 'da', '3D63F235-A433-129F-A5A2-DD0BCCD53BEC',
                'https://www.deviantart.com/u/art/Sitting-Serious-1371636392',
                'posted', 1, 'artwork'),
               ('Perched', 0, 'da', '31EE55E9-F4E2-59BF-5E47-0083B85C9598',
                'https://www.deviantart.com/u/art/Perched-1372058511',
                'posted', 1, 'artwork');
        INSERT INTO masterpiece_members (masterpiece_name, platform, submission_id, linked_via)
        VALUES ('Sitting_Serious', 'da', '3D63F235-A433-129F-A5A2-DD0BCCD53BEC', 'publication'),
               ('Perched', 'da', '1372058511', 'merge'),
               ('Perched', 'da', '31EE55E9-F4E2-59BF-5E47-0083B85C9598', 'publication');
    """)
    conn.commit()


def _members(conn):
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT masterpiece_name, submission_id FROM masterpiece_members WHERE platform='da'")}


def test_the_repair_rekeys_publications_and_members():
    conn = get_connection()
    try:
        _seed_da(conn)
        _da_rekey(conn)
        conn.commit()
        ids = {r[0]: r[1] for r in conn.execute(
            "SELECT story_name, external_id FROM publications WHERE platform='da'")}
        assert ids == {"Sitting_Serious": "1371636392", "Perched": "1372058511"}
        assert ("Sitting_Serious", "1371636392") in _members(conn)
    finally:
        conn.close()


def test_a_duplicate_member_is_dropped_not_collided():
    """`Perched` already had the right integer member from a phash merge. An
    UPDATE would hit the primary key; the GUID row is deleted instead, which is
    also the honest statement — that member was never real."""
    conn = get_connection()
    try:
        _seed_da(conn)
        _da_rekey(conn)
        conn.commit()
        perched = {sid for name, sid in _members(conn) if name == "Perched"}
        assert perched == {"1372058511"}, f"expected exactly one member, got {perched}"
    finally:
        conn.close()


def test_the_repair_is_a_no_op_the_second_time():
    conn = get_connection()
    try:
        _seed_da(conn)
        _da_rekey(conn)
        conn.commit()
        snapshot = _members(conn)
        _da_rekey(conn)
        conn.commit()
        assert _members(conn) == snapshot
    finally:
        conn.close()


def test_an_unresolvable_row_is_left_alone_not_guessed():
    """A wrong id is bad; a guessed one is worse."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO publications (story_name, chapter_index, platform, external_id, "
            "external_url, status, account_id, content_type) "
            "VALUES ('Mystery', 0, 'da', ?, '', 'posted', 1, 'artwork')", (GUID,))
        conn.commit()
        _da_rekey(conn)
        conn.commit()
        assert conn.execute(
            "SELECT external_id FROM publications WHERE story_name='Mystery'"
        ).fetchone()[0] == GUID
    finally:
        conn.close()


def test_the_repair_survives_a_database_without_the_optional_tables():
    """It runs at startup on every install, including ones where
    `masterpiece_members` or `image_hashes` were never created."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _da_rekey(conn)          # no publications table at all
    conn.execute("CREATE TABLE publications (pub_id INTEGER PRIMARY KEY, story_name TEXT, "
                 "platform TEXT, external_id TEXT, external_url TEXT)")
    _da_rekey(conn)          # publications only
    conn.close()


# ── never offer back something we posted ─────────────────────────────

def test_suggestions_skip_our_own_posts():
    """The FurAffinity half of the report, and the general form of the DA one.

    FA ids matched all along; what kept coming back was a hash belonging to
    something we published — a story's cover thumbnail hashing to the artwork it
    was drawn from, and story publishing never creates Masterpiece members at
    all. A publication row is proof we put it there, whatever its content type.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO publications (story_name, chapter_index, platform, external_id, "
            "external_url, status, account_id, content_type) "
            "VALUES ('Sample_Story', 1, 'fa', '64274343', '', 'posted', 1, 'story')")
        conn.commit()
        assert ("fa", "64274343") in mq._our_published_pairs(conn)
    finally:
        conn.close()


def test_a_deleted_publication_becomes_a_candidate_again():
    """If the upload is gone from the platform, a lookalike hash is a genuine
    "did you also post this?" candidate rather than our own post."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO publications (story_name, chapter_index, platform, external_id, "
            "external_url, status, account_id, content_type) "
            "VALUES ('Sample_Story', 0, 'fa', '111', '', 'deleted', 1, 'artwork')")
        conn.commit()
        assert ("fa", "111") not in mq._our_published_pairs(conn)
    finally:
        conn.close()


def test_an_unpublished_id_is_still_offered():
    """The feature has to keep working: an upload made by hand, or before
    PawPoller existed, has no publication row and is exactly what suggestions
    are for."""
    conn = get_connection()
    try:
        assert ("fa", "999999") not in mq._our_published_pairs(conn)
    finally:
        conn.close()


def test_the_suggestion_scan_actually_consults_that_set():
    """Pinning the wiring — the helper existing but never being called would
    leave the reported behaviour exactly as it was."""
    import inspect
    src = inspect.getsource(mq.suggestions)
    assert "_our_published_pairs" in src
    assert "if key in ours" in src
