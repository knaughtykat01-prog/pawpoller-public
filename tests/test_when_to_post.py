"""When to post (spec 015): best windows, site shares, habit vs response, the week grid."""
from datetime import datetime, timezone

from database.db import get_connection
from database import analytics_queries as aq

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)   # a Thursday


def _ib(conn, rows):
    """rows: (iso datetime, views) — Inkbunny pieces."""
    start = conn.execute("SELECT COALESCE(MAX(submission_id), 0) FROM submissions").fetchone()[0]
    for i, (when, views) in enumerate(rows):
        conn.execute("INSERT INTO submissions (submission_id, title, views, favorites_count,"
                     " comments_count, create_datetime) VALUES (?, ?, ?, 1, 0, ?)",
                     (start + i + 1, f"P{start + i}", views, when))
    conn.commit()


def _week(conn):
    """30 typical Monday-10:00 posts, a strong Saturday evening (19–21, 2×), a decent
    Wednesday 15:00 (1.5×), a thin-but-huge Sunday 20:00 (2 posts, 10×)."""
    rows = [("2026-09-28 10:00:00", 100)] * 30
    for h in (19, 20, 21):
        rows += [(f"2026-09-26 {h}:00:00", 200)] * 4
    rows += [("2026-09-30 15:00:00", 150)] * 4
    rows += [("2026-09-27 20:00:00", 1000)] * 2
    _ib(conn, rows)


def _run(**kw):
    conn = get_connection()
    try:
        return aq.get_when_to_post(conn, zone_name="UTC", now=NOW, **kw)
    finally:
        conn.close()


def test_windows_merge_neighbours_and_skip_thin_cells():
    conn = get_connection()
    try:
        _week(conn)
    finally:
        conn.close()
    out = _run()
    w = out["windows"]
    assert [(x["day"], x["h0"], x["h1"]) for x in w] == [(5, 19, 21), (2, 15, 15), (0, 10, 10)]
    assert (w[0]["lift"], w[0]["count"], w[0]["confidence"]) == (2.0, 12, "sure")
    assert w[1]["confidence"] == "early sign"
    # The thin Sunday cell is in the grid but never offered.
    assert out["grid"][6][20] == {"median": 10.0, "count": 2}
    assert not any(x["day"] == 6 for x in w)
    assert _run() == out    # SC-003: same data in, same windows out


def test_gap_agrees_with_the_best_window():
    conn = get_connection()
    try:
        _week(conn)
    finally:
        conn.close()
    gap = _run()["gap"]
    assert gap["best"]["day"] == 5 and gap["best"]["h0"] == 19
    assert (gap["habit_h0"], gap["habit_h1"]) == (10, 10)      # 30 of 50 posts at 10:00
    assert gap["habit_lift"] == 1.0 and not gap["aligned"]
    assert gap["next_at"] == "2026-10-03T19:00:00Z"            # the coming Saturday 19:00


def test_too_little_history_names_no_windows():
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-28 10:00:00", 100)] * 10)
    finally:
        conn.close()
    out = _run()
    assert out["too_few"] and out["windows"] == [] and out["gap"] is None
    assert out["hour"][10]["count"] == 10      # the bars still show what there is


def test_date_only_sites_count_by_weekday_never_by_hour():
    conn = get_connection()
    try:
        for i in range(3):
            conn.execute("INSERT INTO ao3_submissions (submission_id, title, views, posted_at)"
                         " VALUES (?, 'S', 50, '2026-09-26')", (i + 1,))
        conn.commit()
    finally:
        conn.close()
    out = _run()
    assert out["weekday"][5]["count"] == 3
    assert out["timed_posts"] == 0 and sum(c["count"] for row in out["grid"] for c in row) == 0


def test_zone_is_dst_correct():
    """20:00 in Sydney is 10:00Z in winter (AEST) and 09:00Z in summer (AEDT)."""
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-01 10:00:00", 100), ("2026-11-01 09:00:00", 100)])
        out = aq.get_when_to_post(conn, zone_name="Australia/Sydney", span_days=0, now=NOW)
    finally:
        conn.close()
    assert out["hour"][20]["count"] == 2 and out["zone"] == "Australia/Sydney"


def test_unknown_zone_falls_back_to_the_browser_offset():
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-28 10:00:00", 100)])
        out = aq.get_when_to_post(conn, zone_name="Nowhere/Atlantis", tz_offset_minutes=600, now=NOW)
    finally:
        conn.close()
    assert out["hour"][20]["count"] == 1 and out["zone"].startswith("UTC+10:00")


def test_shares_never_mix_score_into_views():
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-28 10:00:00", 300)])
        conn.execute("INSERT INTO e621_submissions (submission_id, title, score, favorites_count,"
                     " comments_count, posted_at) VALUES (1, 'E', 900, 7, 2, '2026-09-28T10:00:00Z')")
        conn.commit()
    finally:
        conn.close()
    sh = _run()["shares"]
    assert [s["code"] for s in sh["views"]["sites"]] == ["ib"] and sh["views"]["total"] == 300
    assert {s["code"]: s["value"] for s in sh["faves"]["sites"]} == {"e621": 7, "ib": 1}


def test_small_sites_fold_into_other():
    totals = {c: 100 - i for i, c in enumerate(["ib", "fa", "ws", "sf", "da", "tw", "pix", "fn"])}
    sites = aq._shares(totals)["sites"]
    assert len(sites) == 7 and sites[-1]["label"] == "Other 2 sites" and sites[-1]["value"] == 94 + 93


def test_kind_filter_uses_the_publication_link():
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-28 10:00:00", 100), ("2026-09-28 11:00:00", 100)])
        conn.execute("INSERT INTO publications (content_type, story_name, platform, external_id)"
                     " VALUES ('story', 'Sample Story', 'ib', '1')")
        conn.commit()
    finally:
        conn.close()
    assert _run(kind="story")["posts"] == 1
    assert _run(kind="artwork")["posts"] == 1      # unlinked Inkbunny → artwork
    assert _run(kind="post")["posts"] == 0


def test_span_and_site_filters():
    conn = get_connection()
    try:
        _ib(conn, [("2026-09-28 10:00:00", 100), ("2025-01-01 10:00:00", 100)])
        conn.execute("INSERT INTO fa_submissions (submission_id, title, views, posted_at)"
                     " VALUES (1, 'F', 10, '2026-09-28 10:00:00')")
        conn.commit()
    finally:
        conn.close()
    assert _run(span_days=90)["posts"] == 2
    assert _run(span_days=0)["posts"] == 3
    one = _run(site="fa")
    assert one["posts"] == 1 and {s["code"] for s in one["sites"]} == {"ib", "fa"}


def test_route_refuses_unknown_filters():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.api import router
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    assert c.get("/api/analytics/when-to-post?kind=poems").status_code == 400
    assert c.get("/api/analytics/when-to-post?site=nope").status_code == 400
    r = c.get("/api/analytics/when-to-post?span=90&tz_offset=600")
    assert r.status_code == 200 and len(r.json()["grid"]) == 7


def test_absurd_span_or_offset_is_clamped_not_a_crash(monkeypatch):
    """WTPCLAMP: span=1e6 overflowed the date and tz_offset past a day made timezone() raise —
    both were 500s. Clamped to 100 years and ±23:59."""
    import config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.api import router
    monkeypatch.setattr(config, "get_settings", lambda: {})
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.get("/api/analytics/when-to-post?span=1000000000&tz_offset=99999")
    assert r.status_code == 200 and r.json()["zone"].startswith("UTC+23:59")
    assert r.json()["filters"]["span_days"] == 36500
    assert c.get("/api/analytics/insights?tz_offset=-99999").status_code == 200


def test_route_prefers_the_browser_zone_name_over_its_offset(monkeypatch):
    """With no zone saved, the browser's zone NAME is used (DST-correct); its fixed
    offset put a summer schedule link an hour out."""
    import config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.api import router
    monkeypatch.setattr(config, "get_settings", lambda: {})
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/api/analytics/when-to-post?tz=Australia/Sydney&tz_offset=600")
    assert r.json()["zone"] == "Australia/Sydney"
