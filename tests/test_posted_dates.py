"""Posted dates are real dates, shown, and found for unlinked pieces (4.3.1).

The 4.0.12 sort was right in intent and wrong in two ways that a real library
showed up at once:

* every platform stores its post date in its OWN string format — FurAffinity
  ``August 7, 2019 11:57:56 PM``, Inkbunny ``2026-01-30 03:09:28+00``, SoFurry
  ``2026-02-19T01:10:53Z``, e621 ``2020-07-02T02:08:10-04:00`` — and both the
  "earliest wins" pick and the shelf sort compared those STRINGS. "September"
  sorts after "July"; any month name sorts after any ``2026-…``. Six-year-old
  pieces sat above last week's.
* most of a bulk-imported library has no link to any site upload at all, so
  it had no date and fell back to the import day — three days for 117 pieces.

Three fixes: normalise every date to one form at the source, match an
unlinked piece to a submission by its title (uniquely, or not at all), and
put the date on the card so the sort key can be seen.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from database.platform_metrics import normalize_posted


class TestNormalize:
    @pytest.mark.parametrize("raw, want", [
        ("August 7, 2019 11:57:56 PM", "2019-08-07 23:57:56"),          # FurAffinity
        ("Aug 8, 2019, 12:01:00 AM", "2019-08-08 00:01:00"),
        ("2026-01-30 03:09:28.559557+00", "2026-01-30 03:09:28"),        # Inkbunny (+00, 6-digit frac)
        ("2026-01-30 03:45:29.17803+00", "2026-01-30 03:45:29"),         # 5-digit fraction — fromisoformat rejects this
        ("2026-02-19T01:10:53.000000Z", "2026-02-19 01:10:53"),          # SoFurry
        ("2020-07-02T02:08:10.164-04:00", "2020-07-02 06:08:10"),        # e621, converted to UTC
        ("2026-07-10T00:09:38+0000", "2026-07-10 00:09:38"),             # Threads / Instagram
        ("2026-08-19T04:18:18.8642+00", "2026-08-19 04:18:18"),
        ("2026-03-06", "2026-03-06 00:00:00"),                            # SquidgeWorld / AO3 date-only
        ("2026-06-12 23:43:59", "2026-06-12 23:43:59"),                   # X / Tumblr / FN
        ("2026-09-03T05:18:56", "2026-09-03 05:18:56"),                   # Telegram
        ("", ""), (None, ""), ("not a date", ""),
    ])
    def test_every_observed_format_becomes_one_form(self, raw, want):
        assert normalize_posted(raw) == want

    def test_the_form_sorts_as_time(self):
        """The whole point: string order == time order after normalisation."""
        raws = ["September 15, 2025 01:08:32 AM", "2026-08-31 05:09:23.476262+00",
                "July 2, 2020 02:30:03 AM", "2026-03-06"]
        out = sorted(normalize_posted(r) for r in raws)
        assert out == ["2020-07-02 02:30:03", "2025-09-15 01:08:32", "2026-03-06 00:00:00", "2026-08-31 05:09:23"]


@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "pd.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


class TestResolverNormalises:
    def test_read_posted_at_returns_normalised_values(self, conn):
        from database import platform_metrics as pm
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'x', 'August 7, 2019 11:57:56 PM')")
        conn.commit()
        assert pm.read_posted_at(conn, "fa", ["1"]) == {"1": "2019-08-07 23:57:56"}

    def test_earliest_is_earliest_in_time_not_in_the_alphabet(self, conn):
        """FA's 'September 2025' string is > IB's '2026-…' string; the piece was
        first posted on FA in 2025 and that must win."""
        from routes.submissions_api import _posted_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'x', 'September 15, 2025 01:08:32 AM')")
        conn.execute("INSERT INTO submissions (submission_id, title, create_datetime)"
                     " VALUES (2, 'x', '2026-01-30 03:09:28.559557+00')")
        conn.commit()
        pubs = [
            {"content_type": "artwork", "story_name": "P", "platform": "fa", "external_id": "1", "status": "posted"},
            {"content_type": "artwork", "story_name": "P", "platform": "ib", "external_id": "2", "status": "posted"},
        ]
        assert _posted_dates(conn, pubs, [])[("artwork", "P")] == "2025-09-15 01:08:32"


class TestTitleFallback:
    def test_an_unlinked_piece_is_dated_by_a_unique_title_match(self, conn):
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'Sample Piece', 'August 7, 2019 11:57:56 PM')")
        conn.commit()
        art = [{"name": "Sample_Piece", "title": "Sample Piece"}]
        assert _title_dates(conn, art, have=set()) == {("artwork", "Sample_Piece"): "2019-08-07 23:57:56"}

    def test_matching_ignores_case_punctuation_and_the_folder_name(self, conn):
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO submissions (submission_id, title, create_datetime)"
                     " VALUES (5, 'sample piece!', '2016-03-01 10:00:00')")
        conn.commit()
        art = [{"name": "Sample_Piece", "title": ""}]      # no title on the record → folder name
        assert _title_dates(conn, art, have=set()) == {("artwork", "Sample_Piece"): "2016-03-01 10:00:00"}

    def test_a_title_that_appears_twice_on_one_site_is_not_used(self, conn):
        """Two 'Commission' uploads: which one is this piece? Neither — guessing
        would put a wrong date on a piece with no way to see it was wrong."""
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'Commission', 'August 7, 2019 11:57:56 PM')")
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (2, 'Commission', 'August 9, 2019 11:57:56 PM')")
        conn.commit()
        assert _title_dates(conn, [{"name": "Commission", "title": "Commission"}], have=set()) == {}

    def test_short_titles_never_match(self, conn):
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'Hi', 'August 7, 2019 11:57:56 PM')")
        conn.commit()
        assert _title_dates(conn, [{"name": "Hi", "title": "Hi"}], have=set()) == {}

    def test_an_announcement_is_not_evidence_of_a_first_post(self, conn):
        """On the first real library the only title matches were two Telegram
        channel posts from that day — old pieces would have been dated 'today'."""
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO tg_submissions (message_id, chat_id, title, posted_at)"
                     " VALUES (7, '-100', 'Sample Piece', '2026-09-03T05:18:56')")
        conn.commit()
        assert _title_dates(conn, [{"name": "Sample_Piece", "title": "Sample Piece"}], have=set()) == {}

    def test_pieces_that_already_have_a_date_are_skipped(self, conn):
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'Sample Piece', 'August 7, 2019 11:57:56 PM')")
        conn.commit()
        art = [{"name": "Sample_Piece", "title": "Sample Piece"}]
        assert _title_dates(conn, art, have={("artwork", "Sample_Piece")}) == {}

    def test_earliest_across_sites_for_a_unique_title(self, conn):
        from routes.submissions_api import _title_dates
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at)"
                     " VALUES (1, 'Sample Piece', 'August 7, 2019 11:57:56 PM')")
        conn.execute("INSERT INTO e621_submissions (submission_id, title, posted_at)"
                     " VALUES (2, 'Sample Piece', '2018-01-01T00:00:00-04:00')")
        conn.commit()
        art = [{"name": "Sample_Piece", "title": "Sample Piece"}]
        assert _title_dates(conn, art, have=set()) == {("artwork", "Sample_Piece"): "2018-01-01 04:00:00"}


class TestWorksCarryTheSource:
    def _works(self, **kw):
        from routes.submissions_api import assemble_works
        base = dict(stories=[], artworks=[], pubs=[], acct_to_persona={}, personas={}, junk={})
        base.update(kw)
        return assemble_works(**base)["works"]

    def test_source_is_named_so_the_ui_can_mark_an_estimate(self):
        w = self._works(
            artworks=[{"name": "A", "title": "A", "created_at": "2026-07-10 00:00:00"},
                      {"name": "B", "title": "B", "created_at": "2026-07-10 00:00:00"},
                      {"name": "C", "title": "C", "created_at": "2026-07-10 00:00:00",
                       "original_posted_at": "August 7, 2019 11:57:56 PM"}],
            posted_dates={("artwork", "A"): "2020-01-01 00:00:00", ("artwork", "B"): "2021-01-01 00:00:00"},
            estimated={("artwork", "B")})
        by = {x["name"]: x for x in w}
        assert by["A"]["posted_date_source"] == "link"
        assert by["B"]["posted_date_source"] == "title"
        assert by["C"]["posted_date_source"] == "record"
        assert by["C"]["original_posted_at"] == "2019-08-07 23:57:56", "a raw record value is normalised on the way out"

    def test_never_posted_is_empty_not_guessed(self):
        w = self._works(artworks=[{"name": "N", "title": "N", "created_at": "2026-07-10 00:00:00"}])
        assert w[0]["original_posted_at"] == "" and w[0]["posted_date_source"] == ""


class TestSurfaces:
    def test_the_importer_stores_the_normalised_form(self):
        src = open("posting/artwork_importer.py", encoding="utf-8").read()
        assert "normalize_posted(" in src

    def test_the_shelf_shows_the_date_it_sorts_by(self):
        js = open("frontend/js/bookshelf.js", encoding="utf-8").read()
        assert "book-posted" in js and "posted_date_source" in js
        assert "≈" in js, "a title-matched date is marked as an estimate"

    def test_both_detail_pages_show_first_posted(self):
        assert "first_posted" in open("routes/masterpieces_api.py", encoding="utf-8").read()
        assert "first_posted" in open("routes/posting_api.py", encoding="utf-8").read()
        assert "First posted" in open("frontend/js/masterpieces.js", encoding="utf-8").read()
        assert "first_posted" in open("frontend/js/bookshelf.js", encoding="utf-8").read()
