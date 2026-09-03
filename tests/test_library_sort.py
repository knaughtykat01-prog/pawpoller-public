"""The Library's "Most recent" sorts on the real post date (4.0.12).

docs/specs/status_and_sort.md §2. Before this, `created_at` was stamped at
IMPORT and the shelf sorted on it, so a bulk import of 174 discovered pieces
put ten-year-old art first — in whatever order the importer walked them.
Stories were worse: their `created_at` was the empty string, which sorts last,
so every story sank below every artwork permanently.

The tester's question — "we do capture the time they were posted, yes?" — had
the answer "yes, and the Library never looked". The date was on the submission
row; the Discovered tab already read it. These tests hold that read in place.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "ls.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


class TestReadPostedAt:
    def test_picks_the_platforms_own_date_column(self, conn):
        """Inkbunny stores create_datetime, FurAffinity posted_at. A naive
        `posted_at` lookup silently returns nothing for Inkbunny — the same
        shape as the FA-seconds bug that dropped FA from date views."""
        from database import platform_metrics as pm
        conn.execute("INSERT INTO submissions (submission_id, title, create_datetime)"
                     " VALUES (11, 'ib piece', '2016-03-01 10:00:00')")
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (22, 'fa piece', '2024-07-04 12:00:00')")
        conn.commit()
        assert pm.read_posted_at(conn, "ib", ["11"]) == {"11": "2016-03-01 10:00:00"}
        assert pm.read_posted_at(conn, "fa", ["22"]) == {"22": "2024-07-04 12:00:00"}

    def test_unknown_ids_and_platforms_are_simply_absent(self, conn):
        from database import platform_metrics as pm
        assert pm.read_posted_at(conn, "fa", ["999"]) == {}
        assert pm.read_posted_at(conn, "nope", ["1"]) == {}
        assert pm.read_posted_at(conn, "fa", []) == {}


class TestPostedDates:
    def test_a_story_gets_its_date_from_its_publications(self, conn):
        """Stories have NO date field on the record. This is the only route."""
        from routes.submissions_api import _posted_dates
        conn.execute("INSERT INTO ao3_submissions (submission_id, title, posted_at)"
                     " VALUES ('5', 'Sample', '2023-01-15 09:00:00')")
        conn.commit()
        pubs = [{"content_type": "story", "story_name": "Sample_Story", "platform": "ao3",
                 "external_id": "5", "status": "posted"}]
        assert _posted_dates(conn, pubs, []) == {("story", "Sample_Story"): "2023-01-15 09:00:00"}

    def test_earliest_wins_across_platforms(self, conn):
        """A piece live on four sites was published once; later ones are
        reposts. "When was this made public" is the honest reading."""
        from routes.submissions_api import _posted_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'x', '2020-05-05 00:00:00')")
        conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at)"
                     " VALUES (2, 'x', '2019-02-02 00:00:00')")
        conn.commit()
        pubs = [
            {"content_type": "artwork", "story_name": "Piece", "platform": "fa", "external_id": "1", "status": "posted"},
            {"content_type": "artwork", "story_name": "Piece", "platform": "ws", "external_id": "2", "status": "posted"},
        ]
        assert _posted_dates(conn, pubs, [])[("artwork", "Piece")] == "2019-02-02 00:00:00"

    def test_import_source_covers_a_piece_with_no_publication_link(self, conn):
        from routes.submissions_api import _posted_dates
        conn.execute("INSERT INTO submissions (submission_id, title, create_datetime)"
                     " VALUES (77, 'x', '2015-11-11 11:11:11')")
        conn.commit()
        art = [{"name": "Old_Piece", "import_source": {"platform": "ib", "submission_id": "77"}}]
        assert _posted_dates(conn, [], art) == {("artwork", "Old_Piece"): "2015-11-11 11:11:11"}

    def test_unposted_publications_do_not_count(self, conn):
        from routes.submissions_api import _posted_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'x', '2020-05-05 00:00:00')")
        conn.commit()
        pubs = [{"content_type": "artwork", "story_name": "P", "platform": "fa",
                 "external_id": "1", "status": "draft"}]
        assert _posted_dates(conn, pubs, []) == {}


class TestAssembleWorks:
    def _works(self, **kw):
        from routes.submissions_api import assemble_works
        base = dict(stories=[], artworks=[], pubs=[], acct_to_persona={}, personas={}, junk={})
        base.update(kw)
        return assemble_works(**base)["works"]

    def test_a_story_is_no_longer_dateless(self):
        """THE second bug: "" sorted every story below every artwork."""
        w = self._works(stories=[{"name": "S", "title": "S"}],
                        posted_dates={("story", "S"): "2022-02-02 00:00:00"})
        assert w[0]["original_posted_at"] == "2022-02-02 00:00:00"

    def test_a_persisted_artwork_date_beats_the_resolved_one(self):
        """The importer writes it going forward; the resolver is for the
        back-catalogue. When both exist the record's own value wins."""
        w = self._works(artworks=[{"name": "A", "title": "A", "created_at": "2026-01-01 00:00:00",
                                   "original_posted_at": "2018-06-06 00:00:00"}],
                        posted_dates={("artwork", "A"): "2019-01-01 00:00:00"})
        assert w[0]["original_posted_at"] == "2018-06-06 00:00:00"

    def test_default_order_is_by_post_date_with_created_at_as_the_floor(self):
        """A hand-made, never-posted piece has no post date. It must sort by
        when it was made rather than fall off the end."""
        w = self._works(
            artworks=[
                {"name": "Imported", "title": "I", "created_at": "2026-09-03 00:00:00",
                 "original_posted_at": "2016-01-01 00:00:00"},
                {"name": "Handmade", "title": "H", "created_at": "2026-08-01 00:00:00"},
            ])
        assert [x["name"] for x in w] == ["Handmade", "Imported"], (
            "the 2016 import must not outrank a piece made last month just "
            "because it was IMPORTED yesterday")


class TestPersistence:
    def test_create_artwork_writes_the_date_only_when_known(self, tmp_path, monkeypatch):
        """An absent key means "never posted anywhere we know of", which is a
        different statement from a recorded empty one."""
        import json
        from posting import artwork_reader
        # The same redirect tests/test_artwork_reader.py uses. ⚠ Guessing the
        # name here with raising=False wrote two folders into the REAL archive
        # on the first run of this test; a wrong attribute name must FAIL.
        arch = tmp_path / "Artwork"
        arch.mkdir()
        monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
        try:
            name = artwork_reader.create_artwork(
                title="Sample Piece", image_filename="a.png", image_bytes=b"\x89PNG",
                original_posted_at="2017-04-04 04:04:04")
        except TypeError as e:
            pytest.fail(f"create_artwork does not accept original_posted_at: {e}")
        folder = next(p for p in tmp_path.rglob("masterpiece.json"))
        meta = json.loads(folder.read_text(encoding="utf-8"))
        assert meta["original_posted_at"] == "2017-04-04 04:04:04"
        assert artwork_reader.load_artwork(name).original_posted_at == "2017-04-04 04:04:04"

    def test_the_importer_passes_the_source_date(self):
        src = open("posting/artwork_importer.py", encoding="utf-8").read()
        i = src.index("artwork_reader.create_artwork(")
        block = src[i:i + 2500]
        assert "original_posted_at=" in block
        assert 'd.get("create_datetime")' in block, "Inkbunny's column must be in the chain"


class TestShelf:
    def test_the_sort_options_say_what_they_do(self):
        js = open("frontend/js/bookshelf.js", encoding="utf-8").read()
        assert '<option value="recent">Recently posted</option>' in js
        assert '<option value="added">Recently added</option>' in js
        assert ">Most recent<" not in js, "the ambiguous label is back"

    def test_recent_reads_the_post_date_and_falls_back(self):
        js = open("frontend/js/bookshelf.js", encoding="utf-8").read()
        assert "b.original_posted_at || b.created_at" in js
        assert "this._sort === 'added'" in js
