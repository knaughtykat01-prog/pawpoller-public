"""Repost Radar (gap-wave-6) — deterministic "resurface your best old artwork".

``get_repost_candidates`` pools each piece's artwork publications across every
platform, sums the polled engagement, gates to pieces you haven't posted in
``min_age_days``, and ranks by a weighted score. There is NO model here — pure
SQL + arithmetic over your own post dates and poll counts. These tests lock in
the three behaviours that matter:

  1. GATING — too-new pieces and zero-engagement pieces never surface.
  2. POOLING — the same piece on two platforms sums its stats and keeps both
     deep-links.
  3. RANKING — the score is faves*3 + views + comments*5, sorted desc.
"""
from database.db import get_connection
from database import posting_queries, analytics_queries as aq


def _seed(conn):
    # IB lives in `submissions`; FA in `fa_submissions`.
    #  Old_Hit — 200d old, strong engagement on FA + IB (should surface, pooled).
    #  Old_Dud — 200d old but never drew a view (must NOT pad the radar).
    #  Fresh   — 5d old, strong engagement (too new to resurface).
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, "
                 "favorites_count, comments_count) VALUES (100,'Hit',500,40,6)")
    conn.execute("INSERT INTO submissions (submission_id, title, views, "
                 "favorites_count, comments_count) VALUES (900,'Hit',300,20,3)")
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, "
                 "favorites_count, comments_count) VALUES (200,'Dud',0,0,0)")
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, "
                 "favorites_count, comments_count) VALUES (300,'New',999,99,9)")

    def pub(name, plat, ext, url, days_ago):
        posting_queries.upsert_publication(
            conn, name, 0, plat, content_type="artwork",
            external_id=ext, external_url=url)
        # upsert stamps first_posted_at = now; override it to age the row.
        conn.execute(
            "UPDATE publications SET first_posted_at = datetime('now', ?) "
            "WHERE story_name = ? AND platform = ? AND content_type = 'artwork'",
            (f"-{days_ago} days", name, plat))

    pub("Old_Hit", "fa", "100", "http://fa/100", 200)
    pub("Old_Hit", "ib", "900", "http://ib/900", 210)
    pub("Old_Dud", "fa", "200", "http://fa/200", 200)
    pub("Fresh",   "fa", "300", "http://fa/300", 5)
    conn.commit()


def test_gates_by_age_and_engagement():
    conn = get_connection()
    try:
        _seed(conn)
        names = [r["name"] for r in aq.get_repost_candidates(conn, min_age_days=60)]
        assert "Old_Hit" in names       # old + engaged → surfaces
        assert "Fresh" not in names      # too new
        assert "Old_Dud" not in names    # old but zero engagement → skipped
    finally:
        conn.close()


def test_pools_across_platforms_and_scores():
    conn = get_connection()
    try:
        _seed(conn)
        hit = next(r for r in aq.get_repost_candidates(conn, min_age_days=60)
                   if r["name"] == "Old_Hit")
        assert hit["views"] == 800        # 500 + 300
        assert hit["faves"] == 60         # 40 + 20
        assert hit["comments"] == 9       # 6 + 3
        assert hit["score"] == 1025       # 60*3 + 800 + 9*5
        assert [p["platform"] for p in hit["platforms"]] == ["fa", "ib"]
        assert all(p["url"] for p in hit["platforms"])
        assert hit["age_days"] >= 200
    finally:
        conn.close()


def test_anchors_on_real_gallery_date_not_import_date():
    """The bug this feature was rewritten around: publications.first_posted_at is
    the PawPoller *import* date (all recent) for back-catalogue art, so age must
    come from the gallery submission's real posted_at instead. Both pieces here
    are 'imported today'; only their true FA upload dates differ."""
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at, "
                     "views, favorites_count, comments_count) "
                     "VALUES (500,'Vintage','2019-08-11 19:17:50',300,30,4)")
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at, "
                     "views, favorites_count, comments_count) "
                     "VALUES (600,'Recent',datetime('now','-3 days'),300,30,4)")
        for name, ext in (("Vintage", "500"), ("Recent", "600")):
            posting_queries.upsert_publication(
                conn, name, 0, "fa", content_type="artwork",
                external_id=ext, external_url="http://fa/" + ext)
        conn.commit()  # import date (first_posted_at) is 'now' for BOTH

        names = {r["name"] for r in aq.get_repost_candidates(conn, min_age_days=90)}
        assert "Vintage" in names       # real FA date is 2019 → old → surfaces
        assert "Recent" not in names     # real FA date is 3 days ago → too new
    finally:
        conn.close()


def test_fa_human_date_with_seconds_parses():
    """FA scrapes 'August 11, 2019 07:17:50 PM' (full month + SECONDS). If the
    parser misses that format the whole platform silently anchors on its import
    date and drops off the radar — exactly the second prod bug. This locks the
    FA-format piece in at its true 2019 age."""
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, posted_at, "
                     "views, favorites_count, comments_count) "
                     "VALUES (700,'Old FA','August 11, 2019 07:17:50 PM',400,25,3)")
        posting_queries.upsert_publication(
            conn, "Old_FA", 0, "fa", content_type="artwork",
            external_id="700", external_url="http://fa/700")
        conn.commit()  # import date is 'now'; only the real FA date makes it old
        rows = aq.get_repost_candidates(conn, min_age_days=365)
        hit = next((r for r in rows if r["name"] == "Old_FA"), None)
        assert hit is not None                 # parsed the FA date → surfaces
        assert hit["age_days"] > 2000          # 2019 → thousands of days old
    finally:
        conn.close()


def test_lowering_age_filter_reveals_newer_pieces():
    conn = get_connection()
    try:
        _seed(conn)
        old = {r["name"] for r in aq.get_repost_candidates(conn, min_age_days=60)}
        young = {r["name"] for r in aq.get_repost_candidates(conn, min_age_days=1)}
        assert "Fresh" in young
        assert "Fresh" not in old
    finally:
        conn.close()
